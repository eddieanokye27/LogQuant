import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import httpx
import pytest
from fixture.core import Config, CATEGORIES, workload, export, validate_ground_truth
from fixture.runner import traffic, audit, failure_probe, experiment

@pytest.mark.parametrize('kwargs',[{'rate':0},{'exposure':-1},{'poll':float('nan')}, {'concurrency':0},
                                  {'category':'lock'}, {'timeout':1}, {'seed':'1'}])
def test_reject_config(kwargs):
    with pytest.raises(ValueError): Config(**kwargs)

def test_seed_reproducibility():
    def sample(seed):
        generator = workload(seed)
        return [next(generator) for _ in range(100)]
    assert sample(1)==sample(1)
    assert sample(1)!=sample(2)
    assert {op[0] for op in sample(1)} == {'GET','POST'}
    assert any(op[1]=='/products/1/quote' for op in sample(1))

def test_export_boundaries_and_restart_files(tmp_path):
    source = tmp_path/'db'
    source.mkdir()
    records = [{'timestamp':f'2026-01-01T00:00:0{i}+00:00','message':'native message'} for i in range(4)]
    (source/'a.json').write_text('\n'.join(json.dumps(r) for r in records[:2])+'\n')
    (source/'b.json').write_text('\n'.join(json.dumps(r) for r in records[2:])+'\n')
    out = tmp_path/'out.jsonl'
    assert export(source,out,records[1]['timestamp'],records[3]['timestamp'],db=True)==2
    assert [json.loads(line) for line in out.read_text().splitlines()]==records[1:3]
    assert export(source,out,records[1]['timestamp'],records[3]['timestamp'],db=True)==2
    assert len(out.read_text().splitlines())==2

def test_malformed_export_invalidates(tmp_path):
    p = tmp_path/'bad.jsonl'
    p.write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        export(p,tmp_path/'out','2026-01-01T00:00:00Z','2026-01-01T00:01:00Z')

def test_empty_export(tmp_path):
    p = tmp_path/'out'
    assert export(tmp_path/'missing',p,'2026-01-01T00:00:00Z','2026-01-01T00:01:00Z')==0
    assert p.read_text()==''

def test_leakage(tmp_path):
    p = tmp_path/'application.jsonl'
    p.write_text(json.dumps({'timestamp':'2026-01-01T00:00:00Z','message':'permission denied for table orders'})+'\n')
    audit(tmp_path)
    p.write_text(json.dumps({'timestamp':'2026-01-01T00:00:00Z','fault_category':CATEGORIES[0]})+'\n')
    with pytest.raises(ValueError): audit(tmp_path)

def test_ground_truth_requires_observation():
    data = {'config':asdict(Config(category=CATEGORIES[0])), 'valid':True,'invalidity_reasons':[],
            'recovery_verified_at':'2026-01-01T00:01:00Z','start':'2026-01-01T00:00:00Z','end':'2026-01-01T00:02:00Z'}
    with pytest.raises(ValueError): validate_ground_truth(data)

def test_bounded_traffic_records_results():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200)),base_url='http://app') as client:
            results=[]
            dropped=await traffic(client,Config(rate=100),.05,workload(1),results,'baseline')
            assert results and all(r['phase']=='baseline' for r in results)
            assert dropped==0
    asyncio.run(check())

def test_http_failure_alone_does_not_verify_lock():
    class Control:
        def state(self, category): return {'blocked_by_holder':False}
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(500)),base_url='http://app') as client:
            result=await failure_probe(client,Control(),Config(category=CATEGORIES[4],poll=.001))
            assert not result['verified']
    asyncio.run(check())

@pytest.mark.parametrize('interrupted',[False,True])
def test_cleanup_and_invalid_metadata_after_failure(tmp_path, interrupted):
    class Control:
        resets=0
        def reset(self): self.resets+=1
        def runtime(self): return {}
        def inject(self, category):
            if interrupted: raise asyncio.CancelledError()
            raise RuntimeError('injection failed')
    control=Control()
    async def probe(client): return {'at':'2026-01-01T00:00:00Z','healthy':True,'requests':[]}
    async def traffic_stub(*args): return 0
    real_sleep=asyncio.sleep
    async def sleep_stub(*args): await real_sleep(0)
    original_open=Path.open
    def redirected_open(self,*args,**kwargs):
        if str(self)=='/evidence/run.lock': return original_open(tmp_path/'lock',*args,**kwargs)
        return original_open(self,*args,**kwargs)
    with patch('fixture.runner.healthy_probe',probe), patch('fixture.runner.traffic',traffic_stub), \
         patch('fixture.runner.asyncio.sleep',sleep_stub), patch('fixture.runner.export',return_value=1), \
         patch('fixture.runner.audit'), patch('fixture.runner.verify_evidence'), patch.object(Path,'open',redirected_open):
        directory,data=asyncio.run(experiment(Config(category=CATEGORIES[0]),tmp_path/'runs',control))
    assert control.resets==2
    assert not data['valid']
    assert data['invalidity_reasons']
    assert (directory/'evaluation'/'ground_truth.json').exists()

def test_application_real_exception_and_generic_response():
    from fastapi.testclient import TestClient
    from fixture.app import app
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,*args): return self
        def fetchone(self): return (12,0)
    records=[]
    with patch('fixture.app.connect',return_value=Connection()), patch('fixture.app.emit',side_effect=lambda **kw:records.append(kw)):
        with TestClient(app) as client:
            response=client.get('/products/1/quote')
    assert response.status_code==500
    assert response.json()=={'detail':'Operation failed'}
    assert records[0]['exception_type']=='ZeroDivisionError'
    assert records[0]['request_id']
    assert all(category not in json.dumps(records) for category in CATEGORIES)

def test_application_order_validation():
    from fastapi.testclient import TestClient
    from fixture.app import app
    with patch('fixture.app.connect') as connect, patch('fixture.app.emit'):
        with TestClient(app) as client:
            assert client.post('/orders',json={'product_id':1,'quantity':0}).status_code==422
        connect.assert_not_called()

def test_docker_start_is_idempotent():
    from fixture.control import Control
    def respond(request):
        if request.method=='GET': return httpx.Response(200,json=[{'Id':'fixture-db','State':'running'}])
        assert request.url.path=='/containers/fixture-db/start'
        return httpx.Response(304)
    transport=httpx.MockTransport(respond)
    with patch('fixture.control.httpx.HTTPTransport',return_value=transport):
        Control().docker('start')

def test_config_split_validation_precedes_mutations(tmp_path):
    from fixture.cli import run_batch
    with pytest.raises(ValueError):
        asyncio.run(run_batch([{'split':'train','config':{'category':CATEGORIES[0]}}],tmp_path/'manifest.json'))
    assert not (tmp_path/'manifest.json').exists()

@pytest.mark.parametrize('category',[None,CATEGORIES[3]])
@pytest.mark.parametrize('failure', [None, 'workload', 'evidence', 'timing'])
def test_complete_lifecycle(category,tmp_path,failure):
    from fixture.core import now
    class Control:
        resets=0
        injected=False
        recovered=False
        def reset(self): self.resets+=1
        def runtime(self): return {}
        def inject(self, value): self.injected=True
        def recover(self, value): self.recovered=True
    control=Control()
    async def healthy(client): return {'at':now(),'healthy':True,'requests':[]}
    async def phase(client,control,cfg,duration,sequence,results,name,data,timing):
        if failure=='timing':
            timing.violation('clock_divergence',86)
            timing.check()
        status=500 if failure=='workload' and name=='exposure' else 200
        # A synthetic complete one-slot phase; lifecycle tests mock only transport/clock.
        from fixture.scheduling import utc
        start=len(results)*2.0
        wall=1760000000+start
        timing.workload.start_phase(name,1,start,wall)
        operation=next(sequence)
        timing.workload.record(name,0,operation,start,utc(wall),start,utc(wall),'emitted')
        results.append({'status':status,'phase':name,'latency_ms':1,'sequence':0,
                        'method':operation[0],'path':operation[1],
                        'scheduled_at':utc(wall),'planned_monotonic':start,
                        'enqueued_at':utc(wall),'enqueued_monotonic':start,
                        'dispatch_monotonic':start,'request_started_monotonic':start,
                        'request_finished_monotonic':start+.001,'scheduler_delay_ms':0,'queue_wait_ms':0})
        if status!=200 and category is None: raise RuntimeError('Healthy workload failed')
        if name=='exposure' and category:
            data['first_observed_failure']=data['last_observed_failure']=now()
        if name=='recovery': data['recovery_verified_at']=now()
    real_sleep=asyncio.sleep
    async def short_sleep(delay): await real_sleep(min(delay,.001))
    original_open=Path.open
    def redirected_open(self,*args,**kwargs):
        if str(self)=='/evidence/run.lock': return original_open(tmp_path/'lock',*args,**kwargs)
        return original_open(self,*args,**kwargs)
    with patch('fixture.runner.healthy_probe',healthy), patch('fixture.runner.run_phase',phase), \
         patch('fixture.runner.asyncio.sleep',short_sleep), patch('fixture.runner.export',return_value=3), \
         patch('fixture.runner.audit'), \
         patch('fixture.runner.verify_evidence',side_effect=ValueError('missing evidence') if failure=='evidence' else None), \
         patch.object(Path,'open',redirected_open):
        directory,data=asyncio.run(experiment(Config(category=category,baseline=.01,exposure=.01,recovery=.01),tmp_path/'runs',control))
    expected_valid = failure not in ('evidence','timing') and not (failure=='workload' and category is None)
    assert data['valid'] == expected_valid
    assert bool(data['invalidity_reasons']) == (not expected_valid)
    assert control.resets==2
    assert (directory/'evaluation/ground_truth.json').exists()
    if failure=='timing': assert data['timing']['violations']

def test_low_request_rate_does_not_extend_phase():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200)),base_url='http://app') as client:
            await asyncio.wait_for(traffic(client,Config(rate=.001),.01,workload(1),[],'baseline'),timeout=.5)
    asyncio.run(check())

@pytest.mark.parametrize('category,application,database',[
    (CATEGORIES[0], 'OperationalError: connection refused', ''),
    (CATEGORIES[0], 'OperationalError: [Errno -2] Name or service not known', ''),
    (CATEGORIES[1], 'password authentication failed', ''),
    (CATEGORIES[2], 'ZeroDivisionError: division by zero', ''),
    (CATEGORIES[3], 'permission denied for table orders', ''),
    (CATEGORIES[4], 'canceling statement due to lock timeout', 'process 12 still waiting for RowExclusiveLock'),
])
def test_fault_evidence_required(tmp_path, category, application, database):
    from fixture.runner import verify_evidence
    app = tmp_path/'application.jsonl'
    db = tmp_path/'postgresql.jsonl'
    app.write_text(json.dumps({'message': application}))
    db.write_text(json.dumps({'message': database}))
    verify_evidence(tmp_path, category)
    app.write_text(json.dumps({'message': 'Operation failed'}))
    with pytest.raises(ValueError, match='application fault evidence'):
        verify_evidence(tmp_path, category)

def test_generic_timeout_does_not_supply_lock_evidence(tmp_path):
    from fixture.runner import verify_evidence
    (tmp_path/'application.jsonl').write_text('{"message":"lock timeout"}')
    (tmp_path/'postgresql.jsonl').write_text('')
    with pytest.raises(ValueError, match='lock-wait evidence'):
        verify_evidence(tmp_path, CATEGORIES[4])

@pytest.mark.parametrize('denied', [False, True])
def test_permission_state_uses_admin_observation_without_extra_failing_insert(denied):
    from fixture.control import Control
    from unittest.mock import MagicMock
    connection=MagicMock()
    connection.__enter__.return_value=connection
    connection.execute.return_value.fetchone.return_value=(not denied,)
    with patch.object(Control,'admin',return_value=connection):
        assert Control().state(CATEGORIES[3])=={'insert_denied':denied}
    assert 'has_table_privilege' in connection.execute.call_args.args[0]
