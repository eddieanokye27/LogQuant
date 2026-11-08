from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from logquant.features import build_windows, normalize_record, normalize_text, prepare_directory, prepare_ingestion
from logquant.features.cli import main
from logquant.features.windowing import ANCHOR
from logquant.ingestion import load_diagnostic_directory
from logquant.ingestion.models import IngestionReport, IngestionResult, LogRecord


def record(stamp="2026-11-08T12:00:00Z", number=1, source="application.jsonl", **values):
    raw={"timestamp":stamp,"message":values.get("message","request"),"original":{"keep":[1,2,3]}}
    return LogRecord(timestamp=datetime.fromisoformat(stamp.replace("Z","+00:00")),
        service="application" if source=="application.jsonl" else "postgresql", severity="INFO",
        message_field="message", source=source, line_number=number, line_id=f"{source}:{number}",
        raw_record=raw, raw_text=json.dumps(raw)+"\r\n", **{"message":"request",**values})


def result(records):
    return IngestionResult(records=tuple(records), report=IngestionReport(mode="strict",complete=True,
        record_count=len(records),sources=(),errors=()))


def test_explicit_variable_rules_and_no_blanket_number_replacement():
    text='request_id="opaque-42" UUID=123e4567-e89b-12d3-a456-426614174000 peer=192.168.1.2:5432 [2001:db8::1]:443 GET /products/123/quote status=500 SQLSTATE=42501 errno=-2 timeout=800ms'
    normalized=normalize_text(text)
    assert 'request_id="<REQUEST_ID>"' in normalized
    assert 'UUID=<UUID>' in normalized
    assert 'peer=<IPV4>:5432 [<IPV6>]:443' in normalized
    assert '/products/<ROUTE_ID>/quote' in normalized
    assert 'status=500 SQLSTATE=42501 errno=-2 timeout=800ms' in normalized
    assert normalize_text(normalized)==normalized


@pytest.mark.parametrize('text,expected',[
    ('::1','<IPV6>'),('::ffff:192.0.2.1','<IPV6>'),('fe80::1%en0','<IPV6>'),
    ('127.0.0.1.','<IPV4>.'),('2001:db8::1.','<IPV6>.'),
    ('999.2.3.4','999.2.3.4'),('1.2.3.4.5','1.2.3.4.5'),
    ('v1.2.3.4 time 21:42:47.963','v1.2.3.4 time 21:42:47.963'),
    ('/products/123e4567-e89b-12d3-a456-426614174000/quote','/products/<ROUTE_ID>/quote'),
    ('/v1/errors/500 /archive/products/123/quote','/v1/errors/500 /archive/products/123/quote'),
    ('GET /products/12/quote?limit=20','GET /products/<ROUTE_ID>/quote?limit=20'),
])
def test_contextual_normalization_cases(text,expected):
    assert normalize_text(text)==expected


def test_pg_contextual_ids_keep_lock_and_latency_evidence():
    text='process 9963 still waiting for RowExclusiveLock on relation 16394 of database 16384 after 100.537 ms'
    assert normalize_text(text,postgresql=True)==(
        'process <PID> still waiting for RowExclusiveLock on relation <RELATION_ID> of database <DATABASE_ID> after 100.537 ms')
    assert normalize_text('Process holding the lock: 9957. Wait queue: 9963, 9962.',postgresql=True)==(
        'Process holding the lock: <PID>. Wait queue: <PID>, <PID>.')
    assert normalize_text('PID 35 exited with exit code 1',postgresql=True)=='PID <PID> exited with exit code 1'
    assert normalize_text('process 500 SQLSTATE 42501')=='process 500 SQLSTATE 42501'


def test_diagnostic_distinctions_and_original_evidence_untouched():
    messages=[('OperationalError','password authentication failed for user "shop_app"','28P01'),
              ('InsufficientPrivilege','permission denied for table orders','42501'),
              ('LockNotAvailable','canceling statement due to lock timeout\nLINE 1: INSERT ($1,$2)','55P03'),
              ('ZeroDivisionError','float division by zero',None)]
    docs=[]
    for i,(exception,message,state) in enumerate(messages,1):
        original=record(number=i,message=message,exception_type=exception,exception_message=message,
                        sqlstate=state,request_id=f'opaque-{i}',route=f'/products/{i}/quote',status=500)
        before=original.model_dump_json()
        feature=normalize_record(original,run_id='one-run')
        assert feature.fields['exception_type']==exception
        assert feature.fields['exception_message']==message
        if state: assert feature.fields['sqlstate']==state
        assert feature.fields['request_id']=='<REQUEST_ID>'
        assert feature.fields['route']=='/products/<ROUTE_ID>/quote'
        assert feature.fields['status']=='500'
        assert original.model_dump_json()==before
        assert original.line_id==feature.line_id
        assert 'opaque-' not in feature.document and 'one-run' not in feature.document
        docs.append(feature.document)
    assert len(set(docs))==4


def test_boundary_alignment_and_exact_half_open_microseconds():
    stamps=['2026-11-08T12:00:29.999999Z','2026-11-08T12:00:30Z','2026-11-08T12:00:59.999999Z','2026-11-08T12:01:00Z']
    run=prepare_ingestion(result([record(t,i) for i,t in enumerate(stamps,1)]),run_id='A')
    assert run.alignment_anchor==ANCHOR
    assert [w.start.isoformat() for w in run.windows]==[
        '2026-11-08T12:00:00+00:00','2026-11-08T12:00:30+00:00','2026-11-08T12:01:00+00:00']
    assert [w.line_ids for w in run.windows]==[
        ('application.jsonl:1',),('application.jsonl:2','application.jsonl:3'),('application.jsonl:4',)]
    assert all(w.end-w.start==timedelta(seconds=30) for w in run.windows)
    for f in run.features:
        assert sum(w.start<=f.timestamp<w.end for w in run.windows)==1


def test_utc_offsets_and_before_epoch_floor_division():
    run=prepare_ingestion(result([record('1969-12-31T18:59:59.999999-05:00')]),run_id='A')
    assert run.windows[0].start==datetime(1969,12,31,23,59,30,tzinfo=timezone.utc)
    assert run.windows[0].end==ANCHOR
    offset=prepare_ingestion(result([record('2026-11-08T07:00:30-05:00')]),run_id='B')
    assert offset.windows[0].start==datetime(2026,11,8,12,0,30,tzinfo=timezone.utc)


def test_partial_edges_empty_interiors_and_unknown_outside_extent():
    run=prepare_ingestion(result([record('2026-11-08T12:00:17Z'),record('2026-11-08T12:02:03Z',2)]),run_id='A')
    assert len(run.windows)==5 and [w.index for w in run.windows if w.empty]==[1,2,3]
    assert all(not w.line_ids and w.document=='' for w in run.windows if w.empty)
    assert run.windows[0].first_observed_bucket and run.windows[-1].last_observed_bucket
    assert run.windows[0].start.second==0 and run.windows[-1].end.second==30
    assert run.observed_first_event.second==17 and run.observed_last_event.second==3
    assert 'collection_start' not in run.model_dump() and 'collection_end' not in run.model_dump()
    assert run.boundary_basis=='observed-events-only'


def test_singleton_and_no_events_do_not_invent_coverage():
    empty=prepare_ingestion(result([]),run_id='A')
    assert empty.windows==() and empty.observed_first_event is None and empty.observed_last_event is None
    one=prepare_ingestion(result([record()]),run_id='A').windows[0]
    assert one.first_observed_bucket and one.last_observed_bucket
    assert one.end-one.start==timedelta(seconds=30)


def test_run_isolation_duplicate_ids_and_incomplete_input_rejected():
    a=normalize_record(record(),run_id='A')
    b=normalize_record(record(number=2),run_id='B')
    with pytest.raises(ValueError,match='different runs'):build_windows([a,b],run_id='A')
    with pytest.raises(ValueError,match='Duplicate'):build_windows([a,a],run_id='A')
    with pytest.raises(ValueError,match='physical line'):
        build_windows([a.model_copy(update={'line_id':'invented'})],run_id='A')
    assert build_windows([a],run_id='A')[0].window_id!=build_windows([b],run_id='B')[0].window_id
    incomplete=IngestionResult(records=(record(),),report=IngestionReport(mode='permissive',complete=False,
        record_count=1,sources=(),errors=()))
    with pytest.raises(ValueError,match='complete'):prepare_ingestion(incomplete,run_id='A')
    with pytest.raises(ValueError,match='identity'):prepare_ingestion(result([]),run_id=' ')


def test_order_independence_ties_duplicates_and_document_multiplicity():
    original=[record(number=2),record(number=1),record(number=1,source='postgresql.jsonl')]
    a=prepare_ingestion(result(original),run_id='A')
    b=prepare_ingestion(result(list(reversed(original))),run_id='A')
    assert a.model_dump_json()==b.model_dump_json()
    assert a.windows[0].line_ids==('application.jsonl:1','application.jsonl:2','postgresql.jsonl:1')
    assert a.windows[0].document.splitlines()==[f.document for f in a.features]
    assert len(a.features)==3  # Identical documents are not deduplicated.


def source_directory(tmp_path):
    source=tmp_path/'diagnostic';source.mkdir()
    (source/'application.jsonl').write_text(json.dumps({'timestamp':'2026-11-08T12:00:17Z','service':'application',
        'severity':'INFO','event':'request','request_id':'opaque','route':'/products/42/quote'})+'\n')
    (source/'postgresql.jsonl').write_text('')
    (source/'ground_truth.json').write_text('DO NOT READ')
    return source


def test_cli_deterministic_artifacts_no_clobber_and_no_source_changes(tmp_path):
    source=source_directory(tmp_path)
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    out=tmp_path/'out';other=tmp_path/'other'
    args=[str(source),'--run-id','A','--output-dir',str(out)]
    assert main(args)==0
    assert main([str(source),'--run-id','A','--output-dir',str(other)])==0
    artifacts={p.name:p.read_bytes() for p in out.iterdir()}
    assert artifacts=={p.name:p.read_bytes() for p in other.iterdir()}
    assert main(args)==1
    assert artifacts=={p.name:p.read_bytes() for p in out.iterdir()}
    assert main(args+['--overwrite'])==0
    assert json.loads((out/'feature-report.json').read_text())['leading_trailing_collection_periods'].startswith('unknown')
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}


def test_cli_rejects_bad_input_symlinks_and_source_output(tmp_path):
    source=source_directory(tmp_path)
    assert main([str(source),'--run-id','A','--output-dir',str(source/'new')])==1
    assert not (source/'new').exists()
    (source/'postgresql.jsonl').unlink()
    (source/'postgresql.jsonl').symlink_to(source/'ground_truth.json')
    out=tmp_path/'out'
    assert main([str(source),'--run-id','A','--output-dir',str(out)])==2
    assert not out.exists()


def test_feature_cli_output_links_cannot_overwrite_evidence(tmp_path):
    source=source_directory(tmp_path)
    original=(source/'application.jsonl').read_bytes()
    out=tmp_path/'out';out.mkdir()
    (out/'features.jsonl').symlink_to(source/'application.jsonl')
    args=[str(source),'--run-id','A','--output-dir',str(out),'--overwrite']
    assert main(args)==1
    assert (source/'application.jsonl').read_bytes()==original
    alias=tmp_path/'alias';alias.symlink_to(out,target_is_directory=True)
    assert main([str(source),'--run-id','A','--output-dir',str(alias),'--overwrite'])==1
