"""Synthetic manifest/transport checks; never starts Docker or research collection."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('campaign',ROOT/'scripts/research_campaign.py')
campaign=importlib.util.module_from_spec(spec);spec.loader.exec_module(campaign)


@pytest.fixture
def inputs():
    parent=json.loads((ROOT/'logquant-fixture/plans/research.json').read_text())
    # Synthetic predecessor: tests must also work without ignored local exports.
    attempts=[dict(slot_id=r['slot_id'],split=r['split'],config=deepcopy(r['config']),
                   attempt=1,status='valid',run_id='legacy-'+r['slot_id'],invalidity_reasons=[])
              for r in parent['runs'][:3]]
    attempts.extend(dict(slot_id='s004',split='train',config=deepcopy(parent['runs'][3]['config']),
        attempt=i,status='invalid',run_id=f'legacy-invalid-s004-{i}',invalidity_reasons=['synthetic predecessor failure'])
        for i in range(1,4))
    prior=dict(schema_version=1,purpose='research',status='stopped',plan_sha256=campaign.PARENT,attempts=attempts)
    auth=campaign.build_authorization(parent,prior,'retained-predecessor-hash')
    attempts=[dict(slot_id=r['slot_id'],split=r['split'],config=r['config'],attempt=1,
                   status='valid',run_id='new-'+r['slot_id'],invalidity_reasons=[]) for r in auth['selected_slots']]
    manifest=dict(purpose='research',acceptance_policy=campaign.POLICY,plan_sha256=campaign.PARENT,
        parent_plan_sha256=campaign.PARENT,parent_plan_complete=False,status='stage_complete',attempts=attempts,
        stage=dict(name='train-development',selected_slot_ids=[r['slot_id'] for r in auth['selected_slots']],
                   status='complete',pending_slot_ids=[]),pending_slot_ids=auth['pending_test_slots'],
        accepted_run_ids={a['slot_id']:a['run_id'] for a in attempts})
    return parent,prior,auth,manifest


def test_explicit_additional_allowance_preserves_exhausted_original_counts(inputs):
    parent,prior,a,m=inputs;original=deepcopy(prior)
    p=campaign.validate_progress(m,a,complete=True)
    assert prior==original and a['baseline_attempt_counts']['s004']==3
    assert a['maximum_cumulative_counts']['s004']==6 and a['maximum_additional_attempts']==57
    assert p['attempts'][3]['new_version_attempt']==1 and p['attempts'][3]['cumulative_attempt']==4
    assert p['attempts'][0]['cumulative_attempt']==2 and len(p['pending_test_slots'])==23
    assert len(a['legacy_accepted'])==3 and not set(a['legacy_accepted'].values())&set(p['accepted_run_ids'].values())


@pytest.mark.parametrize('change',['seed','baseline'])
def test_conflicting_authoritative_parent_or_original_history_rejected(inputs,change):
    parent,prior,a,m=inputs
    if change=='seed':parent['runs'][0]['config']['seed']=999
    else:prior['attempts'].pop()
    with pytest.raises(ValueError):campaign.build_authorization(parent,prior,'hash')


def stopped(m,a):
    m=deepcopy(m);m['attempts']=m['attempts'][:2]
    m['attempts'][1].update(status='invalid',invalidity_reasons=['synthetic timing failure'])
    m.update(status='stopped',accepted_run_ids={'s001':'new-s001'},
        pending_slot_ids=[r['slot_id'] for r in a['selected_slots'][1:]]+a['pending_test_slots'])
    m['stage'].update(status='stopped',pending_slot_ids=m['pending_slot_ids'][:18])
    return m


def test_invalid_attempt_retained_but_incomplete_stage_ineligible(inputs):
    _,_,a,m=inputs;m=stopped(m,a)
    p=campaign.validate_progress(m,a)
    assert p['attempts'][1]['invalidity_reasons']==['synthetic timing failure']
    assert p['cumulative_attempt_counts']['s002']==2 and p['cumulative_attempt_counts']['s004']==3
    with pytest.raises(ValueError,match='incomplete'):campaign.validate_progress(m,a,complete=True)


@pytest.mark.parametrize('change',['test','attempt4','legacy','split','pending','policy'])
def test_campaign_integrity_rejections(inputs,change):
    _,_,a,m=inputs
    if change=='test':m['attempts'][0]['slot_id']='s020'
    elif change=='attempt4':m['attempts'][0]['attempt']=4
    elif change=='legacy':m['attempts'][0]['run_id']=a['legacy_accepted']['s001']
    elif change=='split':m['attempts'][0]['split']='test'
    elif change=='pending':m['pending_slot_ids']=[]
    else:m['acceptance_policy']='acceptance-v2'
    with pytest.raises(ValueError):campaign.validate_progress(m,a,complete=True)


def test_real_host_wrapper_stops_once_with_mocked_transport(tmp_path,monkeypatch,inputs):
    _,_,a,m=inputs;m=stopped(m,a);fixture=tmp_path/'logquant-fixture';research=fixture/'research-runs'
    collection=research/('a'*32);collection.mkdir(parents=True);out=tmp_path/'out';out.mkdir()
    (collection/'manifest.json').write_text(json.dumps(m))
    (fixture/'environment.lock.json').write_text(json.dumps({'images':{s:{'id':'test-image'} for s in ('app','db')}}))
    power=dict(power="Now drawing from 'AC Power'",lid='open',caffeinate_available=True)
    for name in ('preflight-sandbox.json','preflight-execution.json'):(out/name).write_text(json.dumps(power))
    (out/'authorization.json').write_text(json.dumps(a))
    monkeypatch.setattr(campaign,'check_authorization',lambda root,output:a)
    monkeypatch.setattr(campaign,'power_snapshot',lambda:power)
    monkeypatch.setattr(campaign.subprocess,'check_output',lambda command,**kwargs:
        'test-image|running' if command[1]=='inspect' else 'container-id')
    calls=[]
    class Child:
        pid=100;returncode=1
        def poll(self):return 1
    def popen(command,**kwargs):
        calls.append(command);kwargs['stdout'].write('Collection: research-runs/'+'a'*32+'\n');kwargs['stdout'].flush()
        return Child()
    monkeypatch.setattr(campaign.subprocess,'Popen',popen)
    assert campaign.run(tmp_path,out)==1
    assert len(calls)==1 and calls[0][-2:]==['--stage','train-development']
    result=json.loads((out/'collection-result.json').read_text());assert result['status']=='stopped' and result['automatic_retries']==0
    progress=json.loads((collection/'campaign-progress.json').read_text());assert progress['cumulative_attempt_counts']['s002']==2
    with pytest.raises(FileExistsError):campaign.run(tmp_path,out)
    assert len(calls)==1
