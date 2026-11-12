from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,math,runpy

import pytest
from pydantic import ValidationError
from logquant.detection.contracts import ScoringResult,WindowScore
from logquant.thresholds.contracts import DetectorIdentity,SCORE
from logquant.thresholds.whole_run import (WholeRunCase,WholeRunConfig,select,apply,
    save,load,first_candidate,POLICY)
from logquant.thresholds.storage import load_threshold,save_threshold

ID=DetectorIdentity(model_id='synthetic-only-fixed',usage='synthetic-test-only',payload_sha256='test-only')
BASE=datetime(2026,1,1,tzinfo=timezone.utc)


def scores(name,values):
    rows=[]
    for i,value in enumerate(values):
        start=BASE+timedelta(seconds=30*i)
        rows.append(WindowScore(run_id=name,window_id=f'{name}:{start.isoformat()}',start=start,
            end=start+timedelta(seconds=30),line_ids=(f'application.jsonl:{i+1}',) if value is not None else (),
            status='scored' if value is not None else 'empty_document',anomaly_score=value,
            coverage_unknown=i in (0,len(values)-1)))
    return ScoringResult(model_id=ID.model_id,model_usage=ID.usage,score_definition=SCORE,input_sha256={},scores=tuple(rows))


def choose(cases):return select(cases,ID,WholeRunConfig(),[],'synthetic-test-only')


def test_exact_recall_zero_false_alerts_ties_and_strict_boundary():
    cases=[WholeRunCase(scores('healthy',[9,.2,.2,9]),False)]
    cases += [WholeRunCase(scores(f'fault-{i}',[9,.8,.7,9]),True) for i in range(4)]
    cases += [WholeRunCase(scores('fault-null',[9,None,None,9]),True)]
    artifact=choose(list(reversed(cases)))
    assert artifact.status=='selected' and artifact.threshold==.7
    trial=next(t for t in artifact.trials if t.threshold==artifact.threshold)
    assert trial.recall==.8 and trial.faulty_run_count==5 and trial.healthy_candidates==0
    assert trial.missed_faulty_runs==('fault-null',)
    assert artifact==choose(cases)
    assert artifact.timing_flags['fault-null']=='unassessable'
    result=apply(scores('new',[9,.7,.8,.7,9]),ID,artifact)
    assert [w.state for w in result.windows]==['excluded_edge','below_threshold','alert','below_threshold','excluded_edge']
    assert first_candidate(result)==result.candidates[0]


def test_healthy_unscorable_does_not_appear_safe_and_infeasible_is_retained():
    cases=[WholeRunCase(scores('h',[9,None,None,9]),False),WholeRunCase(scores('f',[9,.8,.8,9]),True)]
    a=choose(cases);assert a.status=='unsuccessful' and a.threshold is None and a.trials
    assert a.trials[0].healthy_unscorable_runs==('h',)
    with pytest.raises(ValueError):apply(cases[1].scores,ID,a)
    identical=choose([WholeRunCase(scores('h',[9,.5,.5,9]),False),WholeRunCase(scores('f',[9,.5,.5,9]),True)])
    assert identical.status=='unsuccessful' and all(not t.feasible for t in identical.trials)


def test_duplicate_config_and_fingerprint_rejection(tmp_path):
    c=WholeRunCase(scores('one',[9,.5,.5,9]),True)
    with pytest.raises(ValueError):choose([c,c])
    with pytest.raises(ValidationError):WholeRunConfig(minimum_faulty_run_recall=.7)
    with pytest.raises(ValidationError):WholeRunConfig(maximum_healthy_candidates=1)
    a=choose([WholeRunCase(scores('h',[9,.1,.1,9]),False),c])
    save(a,tmp_path/'v2');assert load(tmp_path/'v2')==a
    with pytest.raises((ValueError,ValidationError)):load_threshold(tmp_path/'v2')
    with pytest.raises(FileExistsError):save(a,tmp_path/'v2')
    with pytest.raises((ValueError,ValidationError)):apply(c.scores,ID.model_copy(update={'payload_sha256':'other'}),a)
    data=json.loads((tmp_path/'v2/calibration.json').read_text());data['threshold']=.4
    (tmp_path/'v2/calibration.json').write_text(json.dumps(data))
    with pytest.raises(ValueError):load(tmp_path/'v2')


def test_first_candidate_uses_recorded_order_without_labels():
    a=choose([WholeRunCase(scores('h',[9,.1,.1,9]),False),WholeRunCase(scores('f',[9,.9,.9,9]),True)])
    alerts=apply(scores('other',[9,.9,.1,.9,9]),ID,a)
    assert first_candidate(alerts).start==BASE+timedelta(seconds=30)
    assert first_candidate(alerts.model_copy(update={'candidates':tuple(reversed(alerts.candidates))})).candidate_id==alerts.candidates[0].candidate_id
    assert first_candidate(apply(scores('empty',[9,.1,.1,9]),ID,a)) is None


def test_timing_boundaries_unknowns_and_half_open_end():
    from logquant.thresholds.whole_run import assess_timing
    point=lambda s:(BASE+timedelta(seconds=s),)*2
    assert assess_timing(None,point(30),point(90))=='unassessable'
    assert assess_timing(point(60),point(30),point(90),positive_overlap_guaranteed=True)=='definite'
    assert assess_timing(point(90),point(30),point(90),positive_overlap_guaranteed=True)=='miss'
    assert assess_timing((BASE+timedelta(seconds=89),BASE+timedelta(seconds=90)),point(30),point(90),positive_overlap_guaranteed=True)=='unassessable'
    assert assess_timing(point(30),point(30),point(90))=='unassessable'  # Contact alone is not guaranteed positive overlap.
    with pytest.raises(ValueError):assess_timing(point(30),point(90),point(60))


def test_installed_cli_whole_run_infeasibility_and_legacy_separation(tmp_path):
    from logquant.training import fit_manifest
    from logquant.detection import save_artifact
    from logquant.detection.cli import main as detector_cli
    from logquant.thresholds.cli import main
    root=Path(__file__).resolve().parents[1]
    train=runpy.run_path(str(root/'scripts/make_synthetic_detector_example.py'))['create_example'](tmp_path/'train')
    selection=runpy.run_path(str(root/'scripts/make_synthetic_threshold_example.py'))['create_example'](tmp_path/'dev')
    bundle=fit_manifest(train);save_artifact(bundle,tmp_path/'detector')
    for name in ('synthetic-dev-healthy','synthetic-dev-faulty'):
        assert detector_cli(['score',str(tmp_path/'dev/collection'/name/'diagnostic'),'--run-id',name,
            '--artifact-dir',str(tmp_path/'detector'),'--output-dir',str(tmp_path/'dev/scores'/name)])==0
    config=tmp_path/'whole-policy.json';config.write_text('{}')
    args=['calibrate',str(selection),'--config',str(config),'--detector-dir',str(tmp_path/'detector'),
        '--output-dir',str(tmp_path/'threshold'),'--policy','whole-run-v2']
    assert main(args)==3
    a=load(tmp_path/'threshold');assert a.detector.usage=='synthetic-test-only' and a.policy==POLICY
    with pytest.raises((ValueError,ValidationError)):load_threshold(tmp_path/'threshold')
    args[-1]='verified-interval-v1';args[args.index('--output-dir')+1]=str(tmp_path/'wrong')
    assert main(args)==1
