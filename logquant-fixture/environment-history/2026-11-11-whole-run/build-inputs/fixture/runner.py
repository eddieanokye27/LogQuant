import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid
import fcntl
import httpx
from fixture.core import CATEGORIES, Config, now, workload, export, save, validate_ground_truth
from fixture.control import Control
from fixture.timing import TimingGuard, LIMITS, clock_sample
from fixture.scheduling import POLICY, WorkloadAccounting, verify_accounting, utc
from fixture.collectors import snapshot, export_ranges, verify_ranges, verify_representation, verify_request_links

PROBE_INTERVAL = 10.0
PROBE_OPERATIONS = [('GET','/ready',None), ('GET','/products',None),
                    ('GET','/products/1/quote',None), ('POST','/orders',{'product_id':1,'quantity':1})]

async def request(client, operation, timeout=4):
    method, path, body = operation
    started = time.monotonic()
    began = now()
    try:
        response = await asyncio.wait_for(client.request(method, path, json=body), timeout)
        result = {'status': response.status_code, 'request_id':response.headers.get('X-Request-ID')}
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        result = {'status': 0, 'error_type': type(exc).__name__}
    finished = time.monotonic()
    return {'at': now(), 'started_at': began, 'method': method, 'path': path,
            'request_started_monotonic': started, 'request_finished_monotonic': finished,
            'latency_ms': (finished-started)*1000, **result}

async def traffic(client, cfg, duration, sequence, results, phase, timing=None, phase_start=None, phase_wall=None):
    queue = asyncio.Queue(maxsize=cfg.concurrency)
    start = time.monotonic() if phase_start is None else phase_start
    # Pair UTC with the actual monotonic phase anchor, not when this task happens to start.
    origin_wall = phase_wall if phase_wall is not None else time.time()-(time.monotonic()-start)
    planned = math.ceil(duration*cfg.rate)
    accounting = timing.workload if timing and timing.workload is not None else WorkloadAccounting(
        cfg.rate,planned,start if timing and timing.whole_run else None)
    if timing and timing.workload is None:
        timing.workload = accounting
    accounting.start_phase(phase, planned, start, origin_wall)
    async def worker():
        while True:
            operation, index, queued, queued_at, delay = await queue.get()
            try:
                dispatch = time.monotonic()
                wait = dispatch-queued
                result = await request(client, operation, cfg.timeout)
                result.update(phase=phase, sequence=index, scheduled_offset_seconds=index/cfg.rate,
                    scheduled_at=datetime.fromtimestamp(origin_wall+index/cfg.rate,timezone.utc).isoformat(),
                    scheduler_delay_ms=delay*1000, queue_wait_ms=wait*1000,
                    dispatch_delay_ms=(dispatch-start-index/cfg.rate)*1000,
                    planned_monotonic=start+index/cfg.rate, enqueued_monotonic=queued,
                    enqueued_at=queued_at, dispatch_monotonic=dispatch)
                results.append(result)
                if timing and wait > LIMITS['queue_wait_seconds']: timing.violation('queue_wait',wait)
            finally:
                queue.task_done()
    workers = [asyncio.create_task(worker()) for _ in range(cfg.concurrency)]
    dropped = 0
    try:
        for index in range(planned):
            operation = next(sequence)  # Consume one seeded operation even for skipped slots.
            due = start+index/cfg.rate
            await asyncio.sleep(max(0,due-time.monotonic()))
            if timing:
                timing.check()
            queued = time.monotonic()
            while queued < due:  # A timer may wake early at the clock's resolution.
                await asyncio.sleep(due-queued)
                queued = time.monotonic()
            queued_at = now()
            delay = max(0,queued-due)
            reason = accounting.skip_reason(due, queued)
            outcome = 'scheduler_skip' if reason else 'emitted'
            if not reason:
                try:
                    queue.put_nowait((operation,index,queued,queued_at,delay))
                except asyncio.QueueFull:
                    dropped += 1
                    outcome = 'queue_full'
            accounting.record(phase, index, operation, due, utc(origin_wall+index/cfg.rate),
                              queued, queued_at, outcome, reason)
            if timing:
                timing.summary['violations'].extend(accounting.violations())
                if outcome == 'queue_full':
                    timing.violation('queue_full',0,phase=phase,sequence=index,dropped_arrivals=1)
                timing.check()
        await asyncio.sleep(max(0,start+duration-time.monotonic()))
        await queue.join()
    finally:
        for task in workers: task.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
    return dropped

async def healthy_probe(client):
    rows = await asyncio.gather(*(request(client,op) for op in PROBE_OPERATIONS))
    return {'at': now(), 'observed_monotonic':time.monotonic(),
            'healthy': all(r['status']==200 for r in rows), 'requests': rows}

async def failure_probe(client, control, cfg):
    # All four operations start in the same order for every category.
    task = asyncio.ensure_future(asyncio.gather(*(request(client,op,cfg.timeout) for op in PROBE_OPERATIONS)))
    state, sample_times = {}, []
    try:
        while True:
            sample_times.append(now())
            current = await asyncio.to_thread(control.state,cfg.category)
            state.update({k: state.get(k,False) or v for k,v in current.items()})
            if task.done(): break
            await asyncio.sleep(cfg.poll)
        rows = await task
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    index = 2 if cfg.category==CATEGORIES[2] else 3 if cfg.category in CATEGORIES[3:] else 0
    outcome = rows[index]
    authenticated = all(rows[i]['status']==200 for i in (0,1,2)) if cfg.category==CATEGORIES[3] else True
    if cfg.category==CATEGORIES[3]: state['authenticated'] = authenticated
    return {'at': now(), 'observed_monotonic':time.monotonic(),
            'verified': bool(state) and all(state.values()) and outcome['status']==500,
            'healthy': all(r['status']==200 for r in rows), 'state': state,
            'request': outcome, 'requests': rows, 'sample_times': sample_times}

async def run_phase(client,control,cfg,duration,sequence,results,phase,data,timing):
    initial = clock_sample();began,wall = initial['mono_after'],initial['wall']
    record = {'phase':phase,'start':initial['at'],'start_monotonic':began,
              'clock_start':initial,'scheduled_seconds':duration,'probes':0}
    timing.summary['phases'].append(record)
    active = asyncio.create_task(traffic(client,cfg,duration,sequence,results,phase,timing,began,wall))
    try:
        for i in range(math.ceil(duration/PROBE_INTERVAL)):
            due = began+i*PROBE_INTERVAL
            await asyncio.sleep(max(0,due-time.monotonic()))
            delay = max(0,time.monotonic()-due)
            if delay > LIMITS['scheduler_delay_seconds']:
                timing.violation('probe_scheduler_delay',delay,phase=phase,sequence=i,
                                 scheduled_offset_seconds=i*PROBE_INTERVAL,planned_monotonic=due)
            verifying = phase=='exposure' and cfg.category is not None
            probe = await failure_probe(client,control,cfg) if verifying else await healthy_probe(client)
            probe.update(phase=phase, scheduled_offset_seconds=i*PROBE_INTERVAL, scheduler_delay_ms=delay*1000)
            data['observations'].append(probe)
            record['probes'] += 1
            if verifying:
                if not probe['verified']: raise RuntimeError('Injection was not independently verified or verification lost')
                data['first_observed_failure'] = data['first_observed_failure'] or probe['at']
                data['last_observed_failure'] = probe['at']
                data['first_observed_failure_monotonic'] = data.get('first_observed_failure_monotonic') or probe.get('observed_monotonic',time.monotonic())
                data['last_observed_failure_monotonic'] = probe.get('observed_monotonic',time.monotonic())
            elif not probe['healthy']:
                raise RuntimeError('Recovery verification failed' if phase=='recovery' else 'Healthy phase probe failed')
            if phase=='recovery':
                data['recovery_verified_at'] = data['recovery_verified_at'] or probe['at']
                data['recovery_verified_monotonic'] = data.get('recovery_verified_monotonic') or probe.get('observed_monotonic',time.monotonic())
            timing.check()
        dropped = await active
        record['dropped_arrivals'] = dropped
        if dropped: raise RuntimeError('Planned workload arrivals dropped')
        if any(r['status']!=200 for r in results if r['phase']==phase) and not (phase=='exposure' and cfg.category):
            raise RuntimeError('Healthy workload failed in '+phase)
        return dropped
    finally:
        if not active.done(): active.cancel()
        await asyncio.gather(active,return_exceptions=True)
        ended = clock_sample()
        record.update(end=ended['at'],end_monotonic=ended['mono_after'],clock_end=ended,
                      wall_seconds=ended['wall']-wall,monotonic_seconds=ended['mono_after']-began)
        if record['monotonic_seconds'] > duration+LIMITS['phase_overrun_seconds']:
            timing.violation('phase_overrun',record['monotonic_seconds']-duration)
        timing.tick()
        timing.check()

def fingerprint():
    paths = sorted(Path('fixture').glob('*.py'))+[Path(p) for p in (
        'requirements.txt','requirements.lock','Dockerfile','compose.yaml',
        'scripts/verify_validation.py') if Path(p).exists()]
    return hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()

async def experiment(cfg,root=Path('runs'),control=None):
    control = control or Control()
    run_id = uuid.uuid4().hex
    directory = root/run_id
    diagnostic,evaluation = directory/'diagnostic',directory/'evaluation'
    diagnostic.mkdir(parents=True);evaluation.mkdir()
    data = {'schema_version':3,'collection_policy':POLICY,'collection_valid':False,
            'timing_status':'unassessable',
            'timing_limitations':'No continuous cross-service clock mapping is certified.',
            'run_id':run_id,'designation':'faulty' if cfg.category else 'healthy',
            'fault_category':cfg.category,'affected_component':('application' if cfg.category==CATEGORIES[2] else 'postgresql') if cfg.category else None,
            'valid':False,'invalidity_reasons':[],'config':asdict(cfg),'start':now(),
            'observations':[],'source_sha256':fingerprint(),'first_observed_failure':None,'last_observed_failure':None,
            'recovery_verified_at':None,'injection_attempt_at':None,'injection_completed_at':None,
            'probe_interval_seconds':PROBE_INTERVAL,'polling_resolution_seconds':cfg.poll,'simulation':False,
            'host_sleep_guard':os.environ.get('HOST_SLEEP_GUARD','unavailable'),
            'mechanism_limitations':'Application exception uses deliberately invalid stored packing quantity.' if cfg.category==CATEGORIES[2] else None}
    results=[];monitor=None;timing=None;before=None
    with Path('/evidence/run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        async with httpx.AsyncClient(base_url='http://app:8000',timeout=cfg.timeout,trust_env=False,
                limits=httpx.Limits(max_connections=cfg.concurrency+4)) as client:
            try:
                await asyncio.to_thread(control.reset)
                data['runtime']=await asyncio.to_thread(control.runtime)
                env=Path('environment.lock.json')
                if env.is_file():
                    data['environment']=json.loads(env.read_text())
                    data['environment_sha256']=hashlib.sha256(env.read_bytes()).hexdigest()
                deadline=time.monotonic()+cfg.verify_timeout
                while True:
                    probe=await healthy_probe(client)
                    if probe['healthy']: break
                    if time.monotonic()>=deadline: raise RuntimeError('Initial readiness verification failed')
                    await asyncio.sleep(cfg.poll)
                await settle_logs()
                data['observation_started_monotonic']=time.monotonic()
                before=await asyncio.to_thread(snapshot,Path('/evidence/application.jsonl'),Path('/evidence/db'))
                data['start']=now()
                timing=TimingGuard(cfg.rate, sum(math.ceil(getattr(cfg,p)*cfg.rate)
                    for p in ('baseline','exposure','recovery')),whole_run=True)
                monitor=asyncio.create_task(timing.monitor())
                sequence=workload(cfg.seed)
                await run_phase(client,control,cfg,cfg.baseline,sequence,results,'baseline',data,timing)
                if cfg.category:
                    data['injection_attempt_at']=now()
                    data['injection_attempt_monotonic']=time.monotonic()
                    await asyncio.to_thread(control.inject,cfg.category)
                    data['injection_completed_at']=now()
                    data['injection_completed_monotonic']=time.monotonic()
                timing.check()
                await run_phase(client,control,cfg,cfg.exposure,sequence,results,'exposure',data,timing)
                if cfg.category and not data['first_observed_failure']: raise RuntimeError('No independently verified failure')
                data['recovery_attempt_at']=now()
                data['recovery_attempt_monotonic']=time.monotonic()
                await asyncio.to_thread(control.recover,cfg.category)
                await run_phase(client,control,cfg,cfg.recovery,sequence,results,'recovery',data,timing)
                data['valid']=True
            except (Exception,asyncio.CancelledError) as exc:
                data['invalidity_reasons'].append(f'{type(exc).__name__}: {exc}')
            finally:
                await asyncio.sleep(3)
                try:
                    await settle_logs()
                    after=await asyncio.to_thread(snapshot,Path('/evidence/application.jsonl'),Path('/evidence/db'))
                    data['observation_finished_monotonic']=time.monotonic()
                    data['observation_monotonic_seconds']=data['observation_finished_monotonic']-data.get('observation_started_monotonic',data['observation_finished_monotonic'])
                    if before is None:raise ValueError('Missing start collector boundary')
                    data['collector_ranges']=await asyncio.to_thread(export_ranges,before,after,diagnostic)
                    data['log_counts']=verify_ranges(data['collector_ranges'],diagnostic)
                    if not data['log_counts']['application']:raise ValueError('Missing application evidence')
                    audit(diagnostic);verify_evidence(diagnostic,cfg.category)
                    if data['valid']:
                        data['representation']=verify_representation(diagnostic,data['observation_monotonic_seconds'])
                        data['request_links']=verify_request_links(diagnostic,results+[r for o in data['observations'] for r in o['requests']])
                except Exception as exc:
                    data['valid']=False;data['invalidity_reasons'].append(f'Export failed: {exc}')
                if monitor:
                    monitor.cancel();await asyncio.gather(monitor,return_exceptions=True)
                    timing.tick();data['timing']=timing.summary
                    if timing.workload is not None:
                        data['timing']['workload'] = timing.workload.snapshot(len(results))
                        if data['valid']:
                            try:
                                verify_accounting(data,results)
                            except (ValueError,KeyError,TypeError) as exc:
                                data['valid']=False
                                data['invalidity_reasons'].append('Workload accounting failed: '+str(exc))
                    if timing.fatal_violations():
                        data['valid']=False
                        data['invalidity_reasons'].append('Timing acceptance failed; see evaluation timing violations')
                data['end']=now()
                try: await asyncio.to_thread(control.reset)
                except Exception as exc:
                    data['valid']=False;data['invalidity_reasons'].append(f'Cleanup failed: {exc}')
                durations=sorted(r['latency_ms'] for r in results)
                data['traffic_summary']={'requests':len(results),'errors':sum(r['status']!=200 for r in results),
                    'dropped_arrivals':sum(p.get('dropped_arrivals',0) for p in data.get('timing',{}).get('phases',[])),
                    'mean_latency_ms':sum(durations)/len(durations) if durations else None,
                    'p95_latency_ms':durations[min(len(durations)-1,int(len(durations)*.95))] if durations else None}
                data['observed_failure_interval']=[data['first_observed_failure'],data['last_observed_failure']]
                data['probe_requests']=sum(len(o['requests']) for o in data['observations'])
                data['collection_valid']=data['valid']
                try:validate_ground_truth(data)
                except ValueError as exc:
                    data['valid']=data['collection_valid']=False
                    data['invalidity_reasons'].append(str(exc))
                save(evaluation/'ground_truth.json',data);save(evaluation/'requests.json',results)
                save(evaluation/'summary.json',{k:data.get(k) for k in ('run_id','valid','invalidity_reasons','traffic_summary','log_counts','probe_requests','timing')})
    return directory,data

async def settle_logs():
    previous = None
    stable = 0
    for _ in range(20):
        sizes = [(str(p), p.stat().st_size) for p in Path('/evidence').rglob('*') if p.is_file()]
        if sizes == previous:
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
        previous = sizes
        await asyncio.sleep(.25)
    raise RuntimeError('Log files did not settle within 5 seconds')

def audit(directory):
    forbidden = ('injection_attempt_at', 'fault_category', 'ALTER ROLE', 'REVOKE INSERT',
                 'LOCK TABLE orders', 'fixture_control', *CATEGORIES)
    for path in directory.glob('*.jsonl'):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if any(term in line for term in forbidden):
                raise ValueError(f'Potential experimental metadata leakage in {path.name}')
            if 'timestamp' not in row:
                raise ValueError('Missing diagnostic timestamp')

def verify_evidence(directory, category):
    """Require ordinary diagnostic support in every valid faulty run."""
    if category is None:
        return
    application = (directory/'application.jsonl').read_text().lower()
    database = (directory/'postgresql.jsonl').read_text().lower()
    required = {
        CATEGORIES[0]: ('operationalerror',),
        CATEGORIES[1]: ('password authentication failed',),
        CATEGORIES[2]: ('zerodivisionerror', 'division by zero'),
        CATEGORIES[3]: ('permission denied for table orders',),
        CATEGORIES[4]: ('lock timeout',),
    }
    if not all(term in application for term in required[category]):
        raise ValueError('Missing ordinary application fault evidence')
    if category == CATEGORIES[0] and not any(term in application for term in (
            'connection', 'name or service not known', 'temporary failure in name resolution',
            'could not translate host name')):
        raise ValueError('Missing database connectivity error')
    if category == CATEGORIES[4] and 'still waiting for' not in database:
        raise ValueError('Missing native database lock-wait evidence')
