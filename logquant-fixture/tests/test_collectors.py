import json
from copy import deepcopy
from pathlib import Path

import pytest

from fixture.collectors import (snapshot,export_ranges,verify_ranges,verify_nonoverlap,
                                verify_representation,verify_request_links,verify_collection_ranges)


def line(timestamp='2026-01-01T00:00:00+00:00',**extra):
    return (json.dumps({'timestamp':timestamp,**extra},ensure_ascii=False)+'\n').encode()


def setup(tmp_path):
    app=tmp_path/'application';app.write_bytes(line(message='earlier é'))
    pg=tmp_path/'db';pg.mkdir();return app,pg


def test_physical_ranges_preserve_clock_reversals_bytes_duplicates_and_new_pg_file(tmp_path):
    app,pg=setup(tmp_path);before=snapshot(app,pg)
    content=line('2025-01-01T00:00:00+00:00',message='é')*2+line('2030-01-01T00:00:00+00:00')
    with app.open('ab') as f:f.write(content)
    native=line('2020-01-01 00:00:00 UTC',message='restart');(pg/'new.json').write_bytes(native)
    after=snapshot(app,pg);out=tmp_path/'diagnostic';out.mkdir()
    proof=export_ranges(before,after,out)
    assert (out/'application.jsonl').read_bytes()==content
    assert (out/'postgresql.jsonl').read_bytes()==native
    assert verify_ranges(proof,out)=={'application':3,'postgresql':1}
    assert proof['sources']['postgresql']['ranges'][0]['new_file']
    with pytest.raises(FileExistsError):export_ranges(before,after,out)


@pytest.mark.parametrize('damage',['truncate','replace','prefix_change','missing','late_write'])
def test_collector_damage_is_explicit_and_not_repaired(tmp_path,damage):
    app,pg=setup(tmp_path);before=snapshot(app,pg)
    if damage=='truncate':app.write_bytes(b'')
    elif damage=='replace':
        other=tmp_path/'replacement';other.write_bytes(app.read_bytes());other.replace(app)
    elif damage=='prefix_change':app.write_bytes(app.read_bytes().replace(b'earlier',b'changed'))
    elif damage=='missing':app.unlink()
    if damage=='missing':
        with pytest.raises(ValueError):snapshot(app,pg)
        return
    after=snapshot(app,pg)
    if damage=='late_write':
        with app.open('ab') as f:f.write(line())
    out=tmp_path/'out';out.mkdir()
    with pytest.raises(ValueError):export_ranges(before,after,out)


def test_partial_line_and_symlink_boundaries_are_rejected(tmp_path):
    app,pg=setup(tmp_path);app.write_bytes(b'{"unfinished":')
    with pytest.raises(ValueError,match='Partial'):snapshot(app,pg)
    app.unlink();target=tmp_path/'target';target.write_bytes(line());app.symlink_to(target)
    with pytest.raises(ValueError,match='symlink'):snapshot(app,pg)


def test_manifest_reconciliation_and_nonoverlap(tmp_path):
    app,pg=setup(tmp_path);proofs=[];out=None
    for i in range(2):
        before=snapshot(app,pg)
        with app.open('ab') as f:f.write(line())
        out=tmp_path/str(i);out.mkdir();proofs.append(export_ranges(before,snapshot(app,pg),out))
    verify_nonoverlap(proofs)
    with pytest.raises(ValueError,match='Overlapping'):verify_nonoverlap([proofs[0],proofs[0]])
    broken=deepcopy(proofs[1]);broken['sources']['application']['ranges'][0]['output_start']=1
    with pytest.raises(ValueError):verify_ranges(broken,out)
    (out/'application.jsonl').write_bytes(b'')
    with pytest.raises(ValueError):verify_ranges(proofs[1],out)


def test_cohort_overlap_is_rejected_before_next_case(tmp_path):
    app,pg=setup(tmp_path);before=snapshot(app,pg)
    with app.open('ab') as f:f.write(line())
    out=tmp_path/'diagnostic';out.mkdir();proof=export_ranges(before,snapshot(app,pg),out)
    prior=tmp_path/'prior/evaluation';prior.mkdir(parents=True)
    (prior/'ground_truth.json').write_text(json.dumps({'collector_ranges':proof}))
    (tmp_path/'manifest.json').write_text(json.dumps({'attempts':[{'run_id':'prior'},{'status':'running'}]}))
    with pytest.raises(ValueError,match='Overlapping'):verify_collection_ranges(tmp_path,proof)


def test_empty_pg_and_new_restart_file_with_old_name_append(tmp_path):
    app,pg=setup(tmp_path);native=pg/'old.json';native.write_bytes(line())
    before=snapshot(app,pg)
    with native.open('ab') as f:f.write(line(message='startup'))
    out=tmp_path/'out';out.mkdir();proof=export_ranges(before,snapshot(app,pg),out)
    assert not proof['sources']['postgresql']['ranges'][0]['new_file']
    assert verify_ranges(proof,out)=={'application':0,'postgresql':1}


def test_representation_gate_and_request_id_matching(tmp_path):
    app=tmp_path/'application.jsonl';pg=tmp_path/'postgresql.jsonl';pg.write_bytes(b'')
    rows=[{'method':'GET','path':'/ready','status':200,'request_id':str(i)} for i in range(4)]
    app.write_bytes(b''.join(line(f'2026-01-01T00:0{i//2}:{(i%2)*30:02d}+00:00',
        method='GET',route='/ready',status=200,request_id=str(i)) for i in range(4)))
    assert verify_representation(tmp_path,90)['nonempty_interior_windows']==2
    assert verify_request_links(tmp_path,rows)['linked']==4
    rows[0]['request_id']=None;assert verify_request_links(tmp_path,rows)['unknown']==1
    with pytest.raises(ValueError):verify_request_links(tmp_path,rows[:-1])
    with app.open('ab') as f:f.write(line('2026-01-02T00:00:00+00:00'))
    with pytest.raises(ValueError,match='representation'):verify_representation(tmp_path,90)
    app.write_bytes(line()*4)
    with pytest.raises(ValueError):verify_representation(tmp_path,90)
