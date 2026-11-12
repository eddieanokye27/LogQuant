"""Review the six fixture-qualification exports, not arbitrary LogQuant inputs.

Uses only the standard library. Writes an evaluation-only report, exits nonzero
on any failed check, and never changes ground truth or diagnostic records.
Run: python3 scripts/verify_validation.py validation-runs/<collection-id>
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sys


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00').replace(' UTC', '+00:00'))


def review_timing(ground, requests):
    """Explicit legacy-v2/v3 audit paths; neither changes original run validity."""
    timing = ground['timing']
    expected_limits = {'heartbeat_gap_seconds':1.0,'wall_monotonic_divergence_seconds':1.0,
                       'scheduler_delay_seconds':.5,'queue_wait_seconds':1.0,'phase_overrun_seconds':6.0}
    if timing['limits'] != expected_limits or timing['violations']:
        raise ValueError('Readiness timing policy missing, changed, or violated')
    if timing['max_heartbeat_gap_seconds']>1 or timing['max_wall_monotonic_divergence_seconds']>1:
        raise ValueError('Continuity summary exceeds frozen limits')
    policy = timing.get('acceptance_policy','acceptance-v2')
    if policy == 'acceptance-v3-distortion':
        fixture_root = str(Path(__file__).resolve().parents[1])
        if fixture_root not in sys.path:
            sys.path.insert(0,fixture_root)
        from fixture.scheduling import verify_accounting
        verify_accounting(ground,requests)
    elif policy != 'acceptance-v2' or 'workload' in timing:
        raise ValueError('Unknown/conflicting workload acceptance policy')
    elif any(r['scheduler_delay_ms']>500 for r in requests):
        raise ValueError('V2 workload dispatch measurements exceed limits')
    if any(r['queue_wait_ms']>1000 for r in requests):
        raise ValueError('Request dispatch measurements exceed limits')
    return policy


def review(collection, research_stage=False):
    manifest = json.loads((collection/'manifest.json').read_text())
    plan = json.loads((collection/'plan.json').read_text())
    expected = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    purpose = 'research' if research_stage else 'validation'
    if plan['purpose'] != purpose or manifest['purpose'] != purpose:
        raise ValueError('Collection purpose differs from explicit review mode')
    if research_stage:
        # The analysis environment provides the same eligibility guard as fit/calibrate.
        # Default six-case qualification continues to use only the standard library.
        from logquant.training.collections import require_completed_collection
        from logquant.training.orchestration import metadata
        require_completed_collection(manifest, collection, 'research', metadata)
    if manifest['status'] != ('stage_complete' if research_stage else 'complete') or manifest['plan_sha256'] != expected:
        raise ValueError('Collection incomplete or plan hash mismatch')
    slots = {row['slot_id']: row for row in plan['runs']}
    attempts = [a for a in manifest['attempts'] if a['status'] == 'valid']
    if not research_stage and len(manifest['attempts']) != len(attempts):
        ledger = json.loads((collection/'regeneration.json').read_text())
        if ledger['parent_plan_sha256'] != expected:
            raise ValueError('Regeneration parent hash differs')
        prior, replacements, identities = {}, [], set()
        for a in manifest['attempts']:
            predecessor = prior.get(a['slot_id'])
            if a['slot_id'] not in slots or a['config'] != slots[a['slot_id']]['config'] or a['split'] != 'unassigned':
                raise ValueError('Regenerated attempt differs from its qualification slot')
            if a.get('run_id') in identities or (predecessor and predecessor['status'] == 'valid'):
                raise ValueError('Duplicate identity or replacement of an accepted run')
            identities.add(a.get('run_id'))
            number = predecessor['attempt']+1 if predecessor else 1
            if a['attempt'] != number or number > 3:
                raise ValueError('Regeneration attempt limit/order violated')
            if predecessor:
                records=[r for r in ledger['replacements'] if r['slot_id']==a['slot_id'] and r['attempt']==number]
                if len(records)!=1: raise ValueError('Missing/duplicate regeneration ledger record')
                r=records[0]
                if (r['predecessor_run_id']!=predecessor.get('run_id') or r['replacement_run_id']!=a.get('run_id')
                        or r['config']!=a['config'] or r['outcome']!=a['status']
                        or r['reason']!=predecessor['invalidity_reasons'] or not r['remedial_action'] or not r['timestamp']):
                    raise ValueError('Regeneration ledger identity/config/reason mismatch')
                replacements.append(r)
            if a['status'] not in ('valid','invalid','incomplete') or (a['status']!='valid' and not a.get('invalidity_reasons')):
                raise ValueError('Unresolved attempt or missing invalidity reason')
            prior[a['slot_id']]=a
        if len(replacements)!=len(ledger['replacements']): raise ValueError('Foreign regeneration entry')
    selected = set(manifest['stage']['selected_slot_ids']) if research_stage else set(slots)
    if len(selected) != (19 if research_stage else 6) or len(attempts) != len(selected):
        raise ValueError('Unexpected number of selected accepted cases')
    if {row['slot_id'] for row in attempts} != selected:
        raise ValueError('Missing or duplicate slot assignment')
    labels = ['Database unavailable', 'Invalid database credentials', 'Application exception',
              'Database permission denied', 'Database lock contention']
    forbidden = ['fault_category', 'injection_attempt_at', 'alter role', 'revoke insert',
                 'lock table orders', 'fixture_control', *[label.lower() for label in labels],
                 collection.name, *slots]
    # Known local values plus environment overrides; never print credential values.
    secrets = {'local-admin-only', 'local-app-only'}
    secrets.update(os.environ[k] for k in ('APP_PASSWORD', 'ADMIN_PASSWORD') if os.environ.get(k))
    env = Path('.env')
    if env.exists():
        for line in env.read_text().splitlines():
            key, _, value = line.partition('=')
            if key.strip() in ('APP_PASSWORD', 'ADMIN_PASSWORD') and value.strip():
                secrets.add(value.strip().strip('\"\''))
    reports, intervals, source_hashes = [], [], set()
    environment_hashes, acceptance_policies = set(), set()
    for attempt in attempts:
        slot = slots[attempt['slot_id']]
        if attempt['status'] != 'valid' or attempt['split'] != (slot['split'] if research_stage else 'unassigned'):
            raise ValueError('Invalid or conflicting selected assignment')
        directory = collection/attempt['run_id']
        ground = json.loads((directory/'evaluation/ground_truth.json').read_text())
        requests = json.loads((directory/'evaluation/requests.json').read_text())
        cfg = slot['config']
        readiness = ground.get('schema_version',1) >= 2
        if readiness:
            timing = ground['timing']
            policy = review_timing(ground,requests)
            if manifest.get('acceptance_policy','acceptance-v2') != policy:
                raise ValueError('Manifest and run acceptance policies conflict')
            acceptance_policies.add(policy)
            for phase in ('baseline','exposure','recovery'):
                observations=[o for o in ground['observations'] if o['phase']==phase]
                offsets=[i*10 for i in range(math.ceil(cfg[phase]/10))]
                if [o['scheduled_offset_seconds'] for o in observations] != offsets:
                    raise ValueError('Probe schedule differs from category-independent policy')
                for o in observations:
                    if [(r['method'],r['path']) for r in o['requests']] != [
                            ('GET','/ready'),('GET','/products'),('GET','/products/1/quote'),('POST','/orders')]:
                        raise ValueError('Probe request mix differs from fixed suite')
                    if o['scheduler_delay_ms']>500: raise ValueError('Probe dispatch delayed')
                measured=next(p for p in timing['phases'] if p['phase']==phase)
                if measured['monotonic_seconds'] < cfg[phase]-.001 or not cfg[phase]-1 <= measured['wall_seconds'] <= cfg[phase]+6:
                    raise ValueError('Phase duration exceeds acceptance bounds')
            expected_probes=sum(math.ceil(cfg[p]/10)*4 for p in ('baseline','exposure','recovery'))
            if ground['probe_requests'] != expected_probes or expected_probes != 120:
                raise ValueError('Unexpected full-duration probe overhead')
            if ground['log_counts']['application'] != len(requests)+expected_probes:
                raise ValueError('Untracked/missing application requests in diagnostic export')
            environment=json.loads((collection/'environment.lock.json').read_text())
            if ground['environment'] != environment:
                raise ValueError('Environment identity changed across collection')
            if ground['environment_sha256'] != hashlib.sha256((collection/'environment.lock.json').read_bytes()).hexdigest():
                raise ValueError('Environment hash differs from its retained snapshot')
            environment_hashes.add(ground['environment_sha256'])
            if ground['runtime']['dependency_lock_sha256'] != environment['dependency_lock_sha256']:
                raise ValueError('Actual dependency lock differs from environment snapshot')
            if ground['host_sleep_guard'] != 'caffeinate':
                raise ValueError('This macOS readiness collection lacks its host sleep guard')
        if ground['config'] != cfg or attempt['config'] != cfg:
            raise ValueError('Run configuration differs from plan')
        if not ground['valid'] or ground['invalidity_reasons'] or not ground['recovery_verified_at']:
            raise ValueError('Ground truth does not confirm validity/recovery')
        if ground['run_id'] != attempt['run_id'] or ground['fault_category'] != cfg['category']:
            raise ValueError('Run identity/category mismatch')
        start, end = timestamp(ground['start']), timestamp(ground['end'])
        if (end-start).total_seconds() < 300:
            raise ValueError('Run shorter than scheduled full duration')
        intervals.append((start, end))
        source_hashes.add(ground['source_sha256'])
        phases = {}
        for phase in ('baseline', 'exposure', 'recovery'):
            rows = [r for r in requests if r['phase'] == phase]
            times = sorted(timestamp(r['at']) for r in rows)
            span = (max(times)-min(times)).total_seconds() if times else 0
            if span < cfg[phase]-2:
                raise ValueError('Insufficient observed traffic coverage for ' + phase)
            errors = sum(r['status'] != 200 for r in rows)
            if (phase != 'exposure' or cfg['category'] is None) and errors:
                raise ValueError('Healthy phase has workload errors')
            if phase == 'exposure' and cfg['category'] and not errors:
                raise ValueError('Fault exposure has no unsuccessful workload requests')
            request_gaps = [(b-a).total_seconds() for a,b in zip(times, times[1:])]
            phases[phase] = {'requests': len(rows), 'errors': errors, 'observed_span_seconds': round(span, 3),
                             'max_completion_gap_seconds': round(max(request_gaps),3) if request_gaps else None}
        counts, examples = {}, []
        sqlstates = Counter()
        native_lock_waits = 0
        for name in ('application', 'postgresql'):
            lines = (directory/'diagnostic'/(name+'.jsonl')).read_text().splitlines()
            counts[name] = len(lines)
            for line in lines:
                row = json.loads(line)
                if name == 'postgresql':
                    if row.get('state_code'): sqlstates[row['state_code']] += 1
                    native_lock_waits += 'still waiting for' in row.get('message', '').lower()
                if not start <= timestamp(row['timestamp']) < end:
                    raise ValueError('Diagnostic timestamp outside export boundary')
                if any(term in line.lower() for term in [*forbidden, ground['run_id']]):
                    raise ValueError('Experimental marker found in diagnostic output')
                if any(secret in line for secret in secrets):
                    raise ValueError('Known credential value found in diagnostic output')
                if {'statement', 'query', 'internal_query'} & row.keys():
                    raise ValueError('SQL statement field found in diagnostic output')
                if name == 'application' and row.get('status', 0) >= 500:
                    example = (row.get('exception_type'), row.get('message'))
                    if example not in examples: examples.append(example)
            if counts[name] != ground['log_counts'][name]:
                raise ValueError('Diagnostic count differs from ground truth')
        observed = [o for o in ground['observations'] if o.get('verified')]
        expected_state = {'Invalid database credentials': '28P01',
                          'Database permission denied': '42501',
                          'Database lock contention': '55P03'}.get(cfg['category'])
        if expected_state and not sqlstates[expected_state]:
            raise ValueError('Expected native PostgreSQL error state missing')
        if cfg['category'] == 'Database lock contention' and not native_lock_waits:
            raise ValueError('Native lock-wait evidence missing')
        if cfg['category']:
            if not observed or not all(all(o['state'].values()) and o['request']['status'] == 500 for o in observed):
                raise ValueError('Independent failure observations missing')
            if not (start <= timestamp(ground['injection_attempt_at']) <= timestamp(ground['first_observed_failure'])
                    <= timestamp(ground['last_observed_failure']) <= timestamp(ground['recovery_attempt_at'])
                    <= timestamp(ground['recovery_verified_at']) < end):
                raise ValueError('Inconsistent fault/recovery boundaries')
        elif ground['first_observed_failure'] or ground['injection_attempt_at']:
            raise ValueError('Healthy run records an injected failure')
        gaps = [(timestamp(b['at'])-timestamp(a['at'])).total_seconds() for a,b in zip(observed, observed[1:])]
        reports.append({'slot_id': slot['slot_id'], 'run_id': ground['run_id'], 'category': cfg['category'],
                        'elapsed_seconds': round((end-start).total_seconds(),3), 'phases': phases,
                        'log_counts': counts, 'verified_failure_samples': len(observed),
                        'additional_application_records': counts['application']-len(requests),
                        'native_database_sqlstates': dict(sqlstates), 'native_lock_wait_records': native_lock_waits,
                        'max_failure_observation_gap_seconds': round(max(gaps),3) if gaps else None,
                        'dropped_arrivals': ground['traffic_summary']['dropped_arrivals'],
                        'recovery_verified_at': ground['recovery_verified_at'], 'ordinary_error_examples': examples})
        if readiness:
            reports[-1].update(timing=ground['timing'],probe_requests=ground['probe_requests'],
                               administrative_state_samples=sum(len(o.get('sample_times',[])) for o in ground['observations']),
                               environment_sha256=ground['environment_sha256'],host_sleep_guard=ground['host_sleep_guard'],
                               max_scheduler_delay_ms=max(r['scheduler_delay_ms'] for r in requests),
                               max_queue_wait_ms=max(r['queue_wait_ms'] for r in requests),
                               max_dispatch_delay_ms=max(r['dispatch_delay_ms'] for r in requests))
    intervals.sort()
    if any(a[1] > b[0] for a,b in zip(intervals, intervals[1:])):
        raise ValueError('Validation run export intervals overlap')
    if len(source_hashes) != 1:
        raise ValueError('Fixture source/configuration changed across validation cases')
    if len(environment_hashes)>1: raise ValueError('Environment fingerprint changed across validation cases')
    if len(acceptance_policies)>1: raise ValueError('Mixed acceptance policies across validation cases')
    return {'status': 'passed', 'plan_sha256': expected, 'source_sha256': next(iter(source_hashes)),
            'acceptance_policy': next(iter(acceptance_policies),None),
            'review_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'checks': ['nineteen selected research cases; test slots pending' if research_stage else 'six full-duration unassigned cases', 'plan/config identity',
                       'healthy and recovery workload outcomes', 'independent failure observations',
                       'bounded nonoverlapping exports', 'diagnostic counts', 'checked leakage markers'],
            'limitations': ['Marker/credential checks are bounded, not proof of all possible leakage.',
                            'Ordinary messages still require human causal review; no RCA performance is measured.'],
            'runs': reports}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('collection', type=Path)
    parser.add_argument('--research-stage', action='store_true', help='Audit completed train-development stage; use analysis Python')
    args = parser.parse_args()
    report = review(args.collection, args.research_stage)
    report_name = 'stage_report' if args.research_stage else 'validation_report'
    destination = args.collection/(report_name+'.json')
    suffix = 1
    while True:
        try:
            with destination.open('x') as stream:
                stream.write(json.dumps(report, indent=2)+'\n')
            break
        except FileExistsError:
            destination=args.collection/(report_name+'-'+str(suffix)+'.json')
            suffix+=1
    print('PASS: selected full-duration exports reviewed; report: ' + str(destination))
