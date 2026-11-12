from copy import deepcopy
import json
from pathlib import Path

import pytest
from logquant.training.collections import require_completed_collection, plan_hash
from logquant.training.orchestration import metadata

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def completed(tmp_path):
    parent=json.loads((ROOT/'logquant-fixture/plans/research.json').read_text())
    (tmp_path/'plan.json').write_text(json.dumps(parent))
    selected=[r for r in parent['runs'] if r['split'] in ('train','development')]
    attempts=[dict(slot_id=r['slot_id'],split=r['split'],attempt=1,status='valid',config=r['config'],
                   run_id='run-'+r['slot_id'],invalidity_reasons=[]) for r in selected]
    return tmp_path,dict(schema_version=1,purpose='research',status='stage_complete',
        plan_sha256=plan_hash(parent),parent_plan_sha256=plan_hash(parent),parent_plan_complete=False,
        stage=dict(name='train-development',selected_slot_ids=[r['slot_id'] for r in selected],
                   pending_slot_ids=[],status='complete'),attempts=attempts,
        accepted_run_ids={a['slot_id']:a['run_id'] for a in attempts},
        pending_slot_ids=[r['slot_id'] for r in parent['runs'] if r['split']=='test'])


def test_complete_selected_stage_eligible_with_pending_test(completed):
    directory,raw=completed
    require_completed_collection(raw,directory,'research',metadata)


@pytest.mark.parametrize('change',['interrupted','running','missing','invalid','hash','parent','split','config',
    'test_slot','duplicate','accepted','pending','stage_pending','stage_name','full_complete','attempt','synthetic'])
def test_stage_eligibility_rejects_integrity_conflicts(completed,change):
    directory,raw=completed
    raw=deepcopy(raw)
    if change=='interrupted':raw['status']='stopped'
    elif change=='running':raw['stage']['status']='running'
    elif change=='missing':raw['attempts'].pop()
    elif change=='invalid':raw['attempts'][0]['status']='invalid'
    elif change=='hash':raw['parent_plan_sha256']='bad'
    elif change=='parent':
        parent=json.loads((directory/'plan.json').read_text());parent['runs'][0]['config']['seed']=999999
        (directory/'plan.json').write_text(json.dumps(parent))
        raw['parent_plan_sha256']=raw['plan_sha256']=plan_hash(parent)
    elif change=='split':raw['attempts'][0]['split']='test'
    elif change=='config':raw['attempts'][0]['config']['rate']=100
    elif change=='test_slot':raw['attempts'][0]['slot_id']='s020'
    elif change=='duplicate':raw['attempts'][1]['run_id']=raw['attempts'][0]['run_id']
    elif change=='accepted':raw['accepted_run_ids'].pop('s001')
    elif change=='pending':raw['pending_slot_ids']=[]
    elif change=='stage_pending':raw['stage']['pending_slot_ids']=['s001']
    elif change=='stage_name':raw['stage']['name']='test'
    elif change=='full_complete':raw['status']='complete'
    elif change=='attempt':raw['attempts'][0]['attempt']=4
    elif change=='synthetic':raw['purpose']='synthetic-test'
    with pytest.raises(ValueError):require_completed_collection(raw,directory,'research',metadata)


def test_legacy_completion_retained_but_stage_markers_cannot_be_stripped(completed):
    directory,raw=completed
    require_completed_collection(dict(purpose='synthetic-test',status='complete'),directory,'synthetic-test',metadata)
    raw.pop('stage');raw['status']='complete'
    with pytest.raises(ValueError):require_completed_collection(raw,directory,'research',metadata)


def test_stage_preserves_invalid_attempt_and_requires_replacement_ledger(completed):
    directory,raw=completed
    predecessor=deepcopy(raw['attempts'][0]);predecessor.update(status='invalid',run_id='invalid-first',invalidity_reasons=['Timing violation'])
    raw['attempts'][0]['attempt']=2
    raw['attempts'].insert(0,predecessor)
    with pytest.raises(OSError):require_completed_collection(raw,directory,'research',metadata)
    ledger=dict(schema_version=1,parent_plan_sha256=raw['parent_plan_sha256'],replacements=[dict(
        slot_id='s001',attempt=2,predecessor_run_id='invalid-first',replacement_run_id='run-s001',
        config=raw['attempts'][1]['config'],outcome='valid',reason=['Timing violation'],
        remedial_action='Reset and recheck readiness; identical planned configuration',timestamp='2026-11-11T14:00:00Z')])
    (directory/'regeneration.json').write_text(json.dumps(ledger))
    require_completed_collection(raw,directory,'research',metadata)
    ledger['replacements'][0]['predecessor_run_id']='wrong'
    (directory/'regeneration.json').write_text(json.dumps(ledger))
    with pytest.raises(ValueError,match='Replacement ledger'):require_completed_collection(raw,directory,'research',metadata)


@pytest.mark.parametrize('value',['stage-is-not-an-object',None,[]])
def test_stage_schema_errors_are_explicit(completed,value):
    directory,raw=completed
    raw['stage']=value
    with pytest.raises(ValueError,match='metadata schema'):
        require_completed_collection(raw,directory,'research',metadata)
