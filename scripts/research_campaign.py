"""Host-only research-v4-01 ledger. Never changes the qualified fixture image."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
NAME = 'research-v4-01'
POLICY = 'acceptance-v4-whole-run'
PARENT = '3505c0a4bfc87d1e69fb519d53bd54973cf8635256d0e33aaa700c61606fcd2a'
PREDECESSOR = '651f74ddc2ae4cf0a033548bc7d11349'
QUALIFICATION = '3a1b44683b84444093606f1f9f9cdac6'
SOURCE = '158ed949c720acbec46d8d88ca074e9a84c44518051ef235c39c6bbec2959e31'
ENVIRONMENT = '3ee6dba29b6d20011f9823006a21dbaa542ad0052a158f32e75ceaa0df77664c'


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def exclusive(path: Path, value: dict) -> None:
    with path.open('x') as stream:
        json.dump(value, stream, indent=2); stream.write('\n')


def canonical(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def build_authorization(parent: dict, predecessor: dict, predecessor_hash: str) -> dict:
    if canonical(parent) != PARENT or predecessor.get('plan_sha256') != PARENT:
        raise ValueError('Parent plan differs from the approved 42-slot plan')
    selected = [r for r in parent['runs'] if r['split'] in ('train', 'development')]
    if [r['slot_id'] for r in selected] != [f's{i:03}' for i in range(1, 20)]:
        raise ValueError('Selection must be exactly s001-s019')
    if predecessor.get('status') != 'stopped':
        raise ValueError('Original stopped history is required')
    baseline = {r['slot_id']: 0 for r in selected}
    for attempt in predecessor['attempts']:
        if attempt['slot_id'] not in baseline:
            raise ValueError('Foreign/test attempt in predecessor')
        baseline[attempt['slot_id']] += 1
    if baseline != {r['slot_id']: 3 if r['slot_id'] == 's004' else 1 if r['slot_id'] in ('s001', 's002', 's003') else 0 for r in selected}:
        raise ValueError('Predecessor counts conflict with the approved amendment')
    legacy = {a['slot_id']: a['run_id'] for a in predecessor['attempts'] if a['status'] == 'valid'}
    if set(legacy) != {'s001', 's002', 's003'}:
        raise ValueError('Legacy accepted assignments differ')
    return dict(schema_version=1, campaign_id=NAME, approved_at=now(),
        authorization='Owner approved the bounded replacement campaign on November 12, 2026; no supervisor approval claimed',
        policy=POLICY, parent_plan_sha256=PARENT, selected_slots=selected,
        pending_test_slots=[r['slot_id'] for r in parent['runs'] if r['split'] == 'test'],
        predecessor_collection=PREDECESSOR, predecessor_manifest_sha256=predecessor_hash,
        predecessor_attempts=predecessor['attempts'], baseline_attempt_counts=baseline,
        legacy_accepted=legacy, legacy_treatment='Preserved valid with original split; excluded from the new primary dataset',
        additional_attempts_per_slot=3, maximum_additional_attempts=57,
        maximum_cumulative_counts={slot: n+3 for slot,n in baseline.items()},
        qualification_collection=QUALIFICATION, source_sha256=SOURCE, environment_sha256=ENVIRONMENT,
        calibration_policy='whole-run-v2', minimum_faulty_run_recall=0.8, maximum_healthy_candidates=0,
        stop_on_invalid_or_incomplete=True, automatic_retries=0, test_execution_authorized=False,
        live_llm_authorized=False, collection_deadline_seconds=7200)


def check_frozen(root: Path) -> None:
    fixture = root/'logquant-fixture'
    paths = sorted((fixture/'fixture').glob('*.py')) + [fixture/p for p in (
        'requirements.txt','requirements.lock','Dockerfile','compose.yaml','scripts/verify_validation.py')]
    if hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest() != SOURCE:
        raise ValueError('Qualified fixture source changed; no rebuild authorized')
    if digest(fixture/'environment.lock.json') != ENVIRONMENT:
        raise ValueError('Qualified frozen environment differs')


def validate_progress(manifest: dict, authorization: dict, *, complete: bool = False) -> dict:
    if (manifest.get('purpose') != 'research' or manifest.get('acceptance_policy') != POLICY
            or manifest.get('plan_sha256') != PARENT or manifest.get('parent_plan_sha256') != PARENT):
        raise ValueError('Campaign manifest identity/policy differs')
    selected = {r['slot_id']: r for r in authorization['selected_slots']}
    if (manifest.get('stage',{}).get('name') != 'train-development'
            or manifest['stage'].get('selected_slot_ids') != list(selected)
            or manifest.get('parent_plan_complete') is not False):
        raise ValueError('Conflicting stage selection')
    counts = Counter(); accepted = {}; seen = set(); rows = []
    for attempt in manifest['attempts']:
        slot = attempt['slot_id']; counts[slot] += 1
        if slot not in selected or slot in accepted:
            raise ValueError('Foreign/test slot or attempt after acceptance')
        if (type(attempt['attempt']) is not int or attempt['attempt'] != counts[slot]
                or counts[slot] > authorization['additional_attempts_per_slot']):
            raise ValueError('New-version attempt limit/order violated')
        if (attempt['config'] != selected[slot]['config'] or attempt['split'] != selected[slot]['split']):
            raise ValueError('Configuration/split differs from parent')
        rid = attempt.get('run_id')
        if rid and (rid in seen or rid in authorization['legacy_accepted'].values()):
            raise ValueError('Duplicate or legacy run identity')
        if rid: seen.add(rid)
        state = attempt['status']
        if state == 'valid':
            if not rid or attempt.get('invalidity_reasons') != []:
                raise ValueError('Accepted attempt lacks valid evidence identity')
            accepted[slot] = rid
        elif state not in ('running','invalid','incomplete') or (state != 'running' and not attempt.get('invalidity_reasons')):
            raise ValueError('Invalid attempt outcome/reasons')
        prior = [a for a in authorization['predecessor_attempts'] if a['slot_id'] == slot]
        rows.append({**attempt, 'new_version_attempt':counts[slot],
            'cumulative_attempt':authorization['baseline_attempt_counts'][slot]+counts[slot],
            'predecessor_collection':PREDECESSOR,
            'legacy_predecessor_run_ids':[a.get('run_id') for a in prior]})
    pending = [s for s in selected if s not in accepted]
    if (manifest.get('accepted_run_ids') != accepted or manifest['stage'].get('pending_slot_ids') != pending
            or manifest.get('pending_slot_ids') != pending+authorization['pending_test_slots']):
        raise ValueError('Accepted/pending assignments differ')
    if complete and (manifest.get('status') != 'stage_complete' or pending or manifest['stage'].get('status') != 'complete'):
        raise ValueError('Campaign stage is incomplete; fitting/calibration forbidden')
    return dict(campaign_id=NAME, collection_status=manifest['status'], attempts=rows,
        accepted_run_ids=accepted, pending_stage_slots=pending,
        pending_test_slots=authorization['pending_test_slots'],
        cumulative_attempt_counts={s:authorization['baseline_attempt_counts'][s]+counts[s] for s in selected})


def prepare(root: Path, output: Path) -> None:
    fixture = root/'logquant-fixture'; check_frozen(root)
    predecessor_path = fixture/'research-runs'/PREDECESSOR/'manifest.json'
    authorization = build_authorization(read(fixture/'plans/research.json'), read(predecessor_path), digest(predecessor_path))
    qualification = fixture/'validation-runs'/QUALIFICATION
    q = read(qualification/'validation_report.json')
    if q['status'] != 'passed' or len(q['runs']) != 6 or q['source_sha256'] != SOURCE:
        raise ValueError('Qualified six-case audit missing or incompatible')
    other = {p.parent.name for p in (fixture/'research-runs').glob('*/manifest.json')}-{PREDECESSOR}
    if other: raise ValueError('Unexpected existing research campaign; review lineage before collection')
    inputs = ['configs/research-detector-v1.json','configs/whole-run-calibration-v2.json',
              'requirements-analysis.lock','docs/whole-run-protocol.md',
              'docs/validation/2026-11-11-replacement-campaign-proposal.md']
    authorization['frozen_inputs'] = {name:digest(root/name) for name in inputs}
    authorization['qualification_report_sha256'] = digest(qualification/'validation_report.json')
    authorization['parent_plan_file_sha256'] = digest(fixture/'plans/research.json')
    if read(root/'configs/whole-run-calibration-v2.json') != dict(minimum_faulty_run_recall=0.8,maximum_healthy_candidates=0):
        raise ValueError('Approved objective differs')
    exclusive(fixture/'research-runs'/f'{NAME}.authorization.json', authorization)
    exclusive(output/'authorization.json', authorization)
    print(json.dumps({'campaign':NAME,'status':'prepared','selected_slots':19,'test_slots':23,'automatic_retries':0}))


def check_authorization(root: Path, output: Path) -> dict:
    fixture=root/'logquant-fixture';check_frozen(root)
    a=read(output/'authorization.json')
    if a != read(fixture/'research-runs'/f'{NAME}.authorization.json'):
        raise ValueError('Campaign authorization copies differ')
    claim=fixture/'research-runs'/f'{NAME}.started.json'
    if claim.exists() and read(claim)['authorization_sha256']!=digest(output/'authorization.json'):
        raise ValueError('Started campaign authorization fingerprint differs')
    if digest(fixture/'research-runs'/PREDECESSOR/'manifest.json') != a['predecessor_manifest_sha256']:
        raise ValueError('Original predecessor changed')
    if digest(fixture/'plans/research.json') != a['parent_plan_file_sha256']:
        raise ValueError('Parent plan changed')
    if any(digest(root/name) != value for name,value in a['frozen_inputs'].items()):
        raise ValueError('Frozen campaign input changed')
    if digest(fixture/'validation-runs'/QUALIFICATION/'validation_report.json') != a['qualification_report_sha256']:
        raise ValueError('Qualification audit changed')
    return a


def power_snapshot() -> dict:
    result={'at':now()}
    for key,command in [('power',['pmset','-g','batt']),('assertions',['pmset','-g','assertions'])]:
        result[key]=subprocess.check_output(command,text=True,timeout=10).strip()
    lid=subprocess.check_output(['ioreg','-r','-k','AppleClamshellState','-d','4'],text=True,timeout=10)
    state=re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)',lid)
    result['lid']='open' if state and state[1]=='No' else 'closed' if state else 'unknown'
    result['caffeinate_available']=Path('/usr/bin/caffeinate').is_file()
    return result


def run(root: Path, output: Path) -> int:
    a=check_authorization(root,output);fixture=root/'logquant-fixture'
    for name in ('preflight-sandbox.json','preflight-execution.json'):
        p=read(output/name)
        if "'AC Power'" not in p['power'] or p['lid']!='open':raise ValueError('Power readings conflict or fail')
    snapshot=power_snapshot();exclusive(output/'preflight-launcher.json',snapshot)
    if "'AC Power'" not in snapshot['power'] or snapshot['lid']!='open' or not snapshot['caffeinate_available']:
        raise ValueError('AC/open-lid/sleep-guard preflight failed; no campaign started')
    environment=read(fixture/'environment.lock.json')
    for service in ('app','db'):
        cid=subprocess.check_output(['docker','compose','-f','compose.yaml','-f','compose.lock.yaml','ps','-q',service],cwd=fixture,text=True,timeout=10).strip()
        if not cid:raise ValueError('Start the frozen fixture first')
        identity,state=subprocess.check_output(['docker','inspect','--format','{{.Image}}|{{.State.Status}}',cid],text=True,timeout=10).strip().split('|')
        if identity!=environment['images'][service]['id'] or state!='running':raise ValueError('Stale/stopped fixture image')
    exclusive(fixture/'research-runs'/f'{NAME}.started.json',dict(at=now(),authorization_sha256=digest(output/'authorization.json')))
    command=['./fixture.sh','batch','plans/research.json','--stage','train-development']
    collection=None;last=None;problem=None;started=time.monotonic()
    with (output/'collection-console.txt').open('x') as stream:
        child=subprocess.Popen(command,cwd=fixture,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        exclusive(output/'active-process.json',dict(pid=child.pid,process_group=child.pid,command=command,at=now()))
        try:
            while True:
                console=(output/'collection-console.txt').read_text()
                match=re.search(r'^Collection: (research-runs/[0-9a-f]{32})$',console,re.M)
                if match and collection is None:
                    collection=fixture/match[1]
                    exclusive(output/'collection-link.json',dict(collection=str(collection.relative_to(root)),at=now()))
                    exclusive(collection/'campaign-authorization.json',a)
                if collection is not None:
                    try:manifest=read(collection/'manifest.json')
                    except json.JSONDecodeError:
                        if child.poll() is not None:raise
                    else:
                        progress=validate_progress(manifest,a)
                        if any(x['attempt']!=1 for x in manifest['attempts']):raise ValueError('Initial batch must not replace attempts')
                        encoded=json.dumps(progress,sort_keys=True)
                        if encoded!=last:
                            with (collection/'campaign-ledger.jsonl').open('a') as ledger:
                                ledger.write(json.dumps(dict(at=now(),**progress))+'\n');ledger.flush();os.fsync(ledger.fileno())
                            temporary=collection/'campaign-progress.tmp'
                            temporary.write_text(json.dumps(progress,indent=2)+'\n');temporary.replace(collection/'campaign-progress.json')
                            last=encoded
                if child.poll() is not None:break
                if time.monotonic()-started>a['collection_deadline_seconds']:raise TimeoutError('Bounded campaign deadline exceeded')
                time.sleep(1)
        except BaseException as exc:
            problem=f'{type(exc).__name__}: {exc}'
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=60)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
    result=dict(ended=now(),exit_code=child.returncode,reason=problem,
        process_monotonic_seconds=time.monotonic()-started,automatic_retries=0,postflight=power_snapshot(),
        collection=str(collection.relative_to(root)) if collection else None)
    if collection is not None:
        manifest=read(collection/'manifest.json');progress=validate_progress(manifest,a)
        if json.dumps(progress,sort_keys=True)!=last:
            with (collection/'campaign-ledger.jsonl').open('a') as ledger:
                ledger.write(json.dumps(dict(at=now(),**progress))+'\n');ledger.flush();os.fsync(ledger.fileno())
            temporary=collection/'campaign-progress.tmp'
            temporary.write_text(json.dumps(progress,indent=2)+'\n');temporary.replace(collection/'campaign-progress.json')
        exclusive(output/'manifest-final.json',manifest);exclusive(output/'accounting-final.json',progress)
        result['status']=manifest['status']
    exclusive(output/'collection-result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='postflight'},indent=2))
    return child.returncode if not problem else 1


def audit(root: Path, output: Path) -> None:
    a=check_authorization(root,output);collection=root/read(output/'collection-link.json')['collection']
    manifest=read(collection/'manifest.json');progress=validate_progress(manifest,a,complete=True)
    from logquant.training.collections import require_completed_collection
    from logquant.training.orchestration import metadata
    require_completed_collection(manifest,collection,'research',metadata)
    if read(collection/'campaign-progress.json')!=progress:raise ValueError('Campaign sidecar differs from manifest')
    report=read(collection/'stage_report.json')
    if report['status']!='passed' or len(report['runs'])!=19 or report['source_sha256']!=SOURCE:
        raise ValueError('Complete nineteen-case collection audit required')
    exclusive(output/'campaign-eligibility.json',dict(status='passed',at=now(),collection=str(collection),
        authorization_sha256=digest(output/'authorization.json'),manifest_sha256=digest(collection/'manifest.json'),
        stage_audit_sha256=digest(collection/'stage_report.json'),**progress))
    print('PASS: complete campaign lineage and existing stage eligibility; test slots pending')


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','run','audit'])
    p.add_argument('--output',type=Path,default=ROOT/'results/research-v4-01');args=p.parse_args()
    if args.action=='prepare':prepare(ROOT,args.output)
    elif args.action=='run':return run(ROOT,args.output)
    else:audit(ROOT,args.output)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
