import asyncio,json
from pathlib import Path
from unittest.mock import patch
import pytest
from fixture.cli import run_plan
from fixture.plans import plan_hash


@pytest.mark.parametrize('interrupted',[False,True])
def test_new_suite_is_one_shot_retains_predecessor_counts_and_stops(tmp_path,monkeypatch,interrupted):
    payload=json.loads(Path('plans/validation-readiness.json').read_text())
    record=json.loads(Path('plans/qualification-whole-run-v1.json').read_text())
    monkeypatch.chdir(tmp_path)
    previous=tmp_path/'validation-runs'/record['predecessor_collection'];previous.mkdir(parents=True)
    old={'purpose':'validation','status':'stopped','plan_sha256':plan_hash(payload),
         'attempts':[{'slot_id':'v001','status':'valid'},{'slot_id':'v002','status':'invalid'}]}
    original=json.dumps(old);(previous/'manifest.json').write_text(original)
    config=tmp_path/'record.json';config.write_text(json.dumps(record));seen=[]
    async def experiment(cfg,root):
        seen.append(cfg.seed)
        if interrupted and len(seen)==2:raise asyncio.CancelledError()
        return root/f'run-{cfg.seed}',{'run_id':f'run-{cfg.seed}','valid':len(seen)==1,
                                      'invalidity_reasons':[] if len(seen)==1 else ['test collection failure']}
    with patch('fixture.cli.experiment',experiment):
        if interrupted:
            with pytest.raises(asyncio.CancelledError):asyncio.run(run_plan(payload,qualification_record=config))
        else:assert asyncio.run(run_plan(payload,qualification_record=config))==1
        with pytest.raises(FileExistsError):asyncio.run(run_plan(payload,qualification_record=config))
    assert seen==[91001,91002]
    assert (previous/'manifest.json').read_text()==original
    current=[p for p in (tmp_path/'validation-runs').glob('*/manifest.json') if p.parent!=previous]
    assert len(current)==1
    m=json.loads(current[0].read_text());assert m['status']=='stopped'
    assert m['previous_attempt_counts']['v001']==1 and m['attempts'][0]['cumulative_attempt']==2
    assert m['attempts'][0]['attempt']==1 and m['cumulative_attempt_counts']['v002']==2
    assert m['cumulative_attempt_counts']['v003']==0


def test_conflicting_qualification_record_is_rejected_before_state(tmp_path,monkeypatch):
    payload=json.loads(Path('plans/validation-readiness.json').read_text())
    record=json.loads(Path('plans/qualification-whole-run-v1.json').read_text());record['parent_plan_sha256']='wrong'
    monkeypatch.chdir(tmp_path);p=tmp_path/'record.json';p.write_text(json.dumps(record))
    with pytest.raises(ValueError):asyncio.run(run_plan(payload,qualification_record=p))
    assert not (tmp_path/'validation-runs').exists()
