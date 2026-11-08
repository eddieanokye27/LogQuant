from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from logquant.ingestion import IngestionError, load_diagnostic_directory
from logquant.ingestion.cli import main


def app(**changes):
    return {"timestamp": "2026-11-08T12:00:00+00:00", "service": "application",
            "severity": "INFO", "event": "request", **changes}


def pg(**changes):
    return {"timestamp": "2026-11-08 12:00:00.000 UTC", "error_severity": "LOG",
            "message": "checkpoint starting: time", **changes}


def directory(tmp_path, application=None, postgresql=None):
    result = tmp_path/"diagnostic"
    result.mkdir()
    for name, rows in (("application.jsonl", [app()] if application is None else application),
                       ("postgresql.jsonl", [] if postgresql is None else postgresql)):
        (result/name).write_text("".join(json.dumps(r)+"\n" for r in rows), encoding="utf-8")
    return result


def test_source_mappings_and_exception_evidence(tmp_path):
    message = "canceling statement due to lock timeout\nLINE 1: INSERT ...\n ^"
    a = app(severity="ERROR", message=message, exception_type="LockNotAvailable",
            request_id="opaque", method="POST", route="/orders", status=500, duration_ms=812.25,
            unknown={"preserve": [1, True, "é"]})
    p = pg(error_severity="ERROR", message="permission denied for table orders", state_code="42501",
           detail="native detail", hint="native hint", context="native context", line_num=987)
    result = load_diagnostic_directory(directory(tmp_path, [a], [p]))
    one, two = result.records
    assert one.message == message and one.exception_message == message
    assert one.exception_type == "LockNotAvailable" and one.event == "request"
    assert one.message_field == "message" and one.request_id == "opaque"
    assert (one.method, one.route, one.status, one.duration_ms) == ("POST", "/orders", 500, 812.25)
    assert one.raw_record == a
    assert (two.service, two.severity, two.sqlstate) == ("postgresql", "ERROR", "42501")
    assert (two.detail, two.hint, two.context) == ("native detail", "native hint", "native context")
    assert two.line_number == 1 and two.raw_record["line_num"] == 987


def test_event_fallback_is_exact_and_does_not_invent_exception_content(tmp_path):
    result = load_diagnostic_directory(directory(tmp_path, [app(exception_type="ZeroDivisionError")]))
    record = result.records[0]
    assert record.message == "request" and record.message_field == "event"
    assert record.exception_type == "ZeroDivisionError" and record.exception_message is None
    assert record.sqlstate is None and "message" not in record.raw_record


@pytest.mark.parametrize("stamp,expected", [
    ("2026-11-08T07:00:00-05:00", "2026-11-08T12:00:00+00:00"),
    ("2026-11-08T13:30:00+01:30", "2026-11-08T12:00:00+00:00"),
    ("2026-11-08T12:00:00.123456Z", "2026-11-08T12:00:00.123456+00:00"),
])
def test_utc_conversion_preserves_original_timestamp(tmp_path, stamp, expected):
    record = load_diagnostic_directory(directory(tmp_path, [app(timestamp=stamp)])).records[0]
    assert record.timestamp == datetime.fromisoformat(expected)
    assert record.timestamp.tzinfo == timezone.utc
    assert record.raw_record["timestamp"] == stamp


def test_pg_native_utc_and_offset(tmp_path):
    records = load_diagnostic_directory(directory(tmp_path, postgresql=[
        pg(timestamp="2026-11-08 12:00:00.123 UTC"),
        pg(timestamp="2026-11-08T13:00:00.123+01:00")])).records
    assert records[1].timestamp == records[2].timestamp


def test_sqlstate_does_not_infer_severity_exception_or_fault_label(tmp_path):
    record = load_diagnostic_directory(directory(tmp_path, postgresql=[
        pg(state_code="55P03", message='skipping analyze of "orders" --- lock not available')])).records[1]
    assert record.severity == "LOG" and record.sqlstate == "55P03"
    assert record.exception_type is None
    assert "fault_category" not in record.model_dump()


def test_message_without_event_and_duplicate_unicode_json_keys(tmp_path):
    row=app(message="original message")
    row.pop("event")
    source=directory(tmp_path,[row])
    assert load_diagnostic_directory(source).records[0].message == "original message"
    (source/"application.jsonl").write_bytes(b'{"\\ud800":1,"\\ud800":2}\n')
    out=tmp_path/"report"
    assert main([str(source),"--output-dir",str(out)]) == 2
    assert json.loads((out/"ingestion-report.json").read_text())["errors"][0]["kind"] == "json"


def test_ids_before_sorting_and_equal_timestamp_order(tmp_path):
    source = directory(tmp_path, [app(timestamp="2026-11-08T13:00:00Z"), app(), app()], [pg(), pg()])
    result = load_diagnostic_directory(source)
    assert [r.line_id for r in result.records] == [
        "application.jsonl:2", "application.jsonl:3", "postgresql.jsonl:1", "postgresql.jsonl:2", "application.jsonl:1"]
    assert len({r.line_id for r in result.records}) == 5
    assert [r.model_dump() for r in result.records] == [r.model_dump() for r in load_diagnostic_directory(source).records]
    # Identical real events retain distinct physical provenance; nothing is deduplicated.
    assert result.records[0].raw_record == result.records[1].raw_record


def test_raw_text_bytes_crlf_whitespace_unicode_and_no_final_newline(tmp_path):
    source = directory(tmp_path)
    first = "  "+json.dumps(app(message="café\nline two"), ensure_ascii=False)+"  \r\n"
    second = json.dumps(app())
    original = (first+second).encode("utf-8")
    (source/"application.jsonl").write_bytes(original)
    result = load_diagnostic_directory(source)
    assert b"".join(r.raw_text.encode("utf-8") for r in result.records) == original
    assert result.report.sources[0].sha256 == hashlib.sha256(original).hexdigest()
    assert (source/"application.jsonl").read_bytes() == original
    assert result.records[0].message == "café\nline two"


def test_empty_database_is_valid_but_missing_database_is_incomplete(tmp_path):
    source = directory(tmp_path)
    result = load_diagnostic_directory(source)
    database = result.report.sources[1]
    assert result.complete and database.state == "read" and database.physical_lines == 0
    assert database.sha256 == hashlib.sha256(b"").hexdigest()
    (source/"postgresql.jsonl").unlink()
    with pytest.raises(IngestionError) as error:
        load_diagnostic_directory(source)
    assert error.value.result.report.sources[1].state == "missing"
    assert error.value.result.report.errors[0].source == "postgresql.jsonl"
    assert error.value.result.report.errors[0].line_number is None
    permissive = load_diagnostic_directory(source, permissive=True)
    assert not permissive.complete and len(permissive.records) == 1


def test_empty_or_missing_application_is_not_a_healthy_result(tmp_path):
    source = directory(tmp_path, [])
    with pytest.raises(IngestionError, match="empty"):
        load_diagnostic_directory(source)
    (source/"application.jsonl").unlink()
    with pytest.raises(IngestionError, match="missing"):
        load_diagnostic_directory(source)


@pytest.mark.parametrize("bad,kind", [
    (b"{broken\n", "json"), (b"\n", "json"), (b"[]\n", "json"),
    (b'{"timestamp":"x","timestamp":"y"}\n', "json"),
    (b'{"value":NaN}\n', "json"), (b'{"value":1e999}\n', "json"), (b"\xff\n", "encoding"),
    (json.dumps(app(message="\ud800")).encode()+b"\n", "encoding"),
    (json.dumps(app(timestamp=None)).encode()+b"\n", "schema"),
    (json.dumps(app(timestamp="2026-11-08T12:00:00")).encode()+b"\n", "timezone"),
    (json.dumps(app(timestamp="2026-11-08T12:00:00 EST")).encode()+b"\n", "timestamp"),
    (json.dumps(app(timestamp="2026-99-08T12:00:00Z")).encode()+b"\n", "timestamp"),
    (json.dumps(app(timestamp="2026-11-08T12:00:00.123456789Z")).encode()+b"\n", "timestamp"),
    (json.dumps(app(status=True)).encode()+b"\n", "schema"),
    (json.dumps(app(duration_ms="10")).encode()+b"\n", "schema"),
    (json.dumps(app(severity="error")).encode()+b"\n", "schema"),
])
def test_errors_keep_physical_line_numbers_and_explicit_incomplete_state(tmp_path, bad, kind):
    source = directory(tmp_path)
    good = (json.dumps(app())+"\n").encode()
    (source/"application.jsonl").write_bytes(good+bad+good)
    with pytest.raises(IngestionError) as exc:
        load_diagnostic_directory(source)
    result = exc.value.result
    assert not result.complete and len(result.records) == 2
    issue = result.report.errors[0]
    assert (issue.source, issue.line_number, issue.kind) == ("application.jsonl", 2, kind)
    assert issue.reason
    permissive = load_diagnostic_directory(source, permissive=True)
    assert not permissive.complete and permissive.report.mode == "permissive"
    assert [r.line_id for r in permissive.records] == ["application.jsonl:1", "application.jsonl:3"]


@pytest.mark.parametrize("remove", ["timestamp", "service", "severity", "event"])
def test_application_required_fields(tmp_path, remove):
    row = app()
    row.pop(remove)
    with pytest.raises(IngestionError):
        load_diagnostic_directory(directory(tmp_path, [row]))


@pytest.mark.parametrize("change", [{"message":None}, {"error_severity":"UNKNOWN"}, {"state_code":42501}, {"timestamp":None}])
def test_pg_invalid_fields_are_reported(tmp_path, change):
    with pytest.raises(IngestionError) as exc:
        load_diagnostic_directory(directory(tmp_path, postgresql=[pg(**change)]))
    assert exc.value.result.report.errors[0].source == "postgresql.jsonl"
    assert exc.value.result.report.errors[0].line_number == 1


def test_only_allowlisted_files_open_and_no_run_recursion(tmp_path):
    source = directory(tmp_path)
    evaluation = tmp_path/"evaluation"
    evaluation.mkdir()
    (evaluation/"ground_truth.json").write_text("NEVER READ THIS")
    (source/"ground_truth.json").write_text("NEVER READ THIS")
    nested = source/"nested"
    nested.mkdir()
    (nested/"application.jsonl").write_text("NEVER READ THIS")
    opened=[]
    original = os.open
    def spy(path, flags, *args, **kwargs):
        if not flags & os.O_DIRECTORY:
            opened.append(str(path))
        return original(path, flags, *args, **kwargs)
    with patch("logquant.ingestion.boundary.os.open", side_effect=spy):
        assert load_diagnostic_directory(source).complete
    assert opened == ["application.jsonl", "postgresql.jsonl"]
    with pytest.raises(IngestionError):
        load_diagnostic_directory(tmp_path)  # Does not discover or descend into diagnostic/.


@pytest.mark.parametrize("target", ["file", "directory", "ancestor", "ancestor-before-dotdot"])
def test_symlink_boundaries_rejected_without_following(tmp_path, target):
    source = directory(tmp_path)
    evaluation = tmp_path/"evaluation"
    evaluation.mkdir()
    secret = evaluation/"ground_truth.json"
    secret.write_text("SECRET EVALUATION SENTINEL")
    path = source
    if target == "file":
        (source/"application.jsonl").unlink()
        (source/"application.jsonl").symlink_to(secret)
    elif target == "directory":
        path = tmp_path/"linked-diagnostic"
        path.symlink_to(source, target_is_directory=True)
    else:
        alias=tmp_path/"alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        path=alias/"diagnostic" if target == "ancestor" else alias/".."/tmp_path.name/"diagnostic"
    with pytest.raises(IngestionError) as exc:
        load_diagnostic_directory(path)
    assert any(i.kind == "boundary" for i in exc.value.result.report.errors)
    assert "SECRET" not in str(exc.value)


def test_nonregular_input_does_not_block(tmp_path):
    source=directory(tmp_path)
    (source/"postgresql.jsonl").unlink()
    os.mkfifo(source/"postgresql.jsonl")
    with pytest.raises(IngestionError, match="regular"):
        load_diagnostic_directory(source)


def test_cli_separate_outputs_no_clobber_and_explicit_overwrite(tmp_path):
    source = directory(tmp_path, [app(message="keep\nexception", exception_type="RealException")])
    before = {p.name:p.read_bytes() for p in source.iterdir()}
    out = tmp_path/"output"
    args=[str(source), "--output-dir", str(out)]
    assert main(args) == 0
    saved = {p.name:p.read_bytes() for p in out.iterdir()}
    normalized = json.loads((out/"records.jsonl").read_text().splitlines()[0])
    assert normalized["raw_text"].encode() == before["application.jsonl"]
    assert normalized["exception_message"] == "keep\nexception"
    assert main(args) == 1
    assert {p.name:p.read_bytes() for p in out.iterdir()} == saved
    assert main(args+["--overwrite"]) == 0
    assert {p.name:p.read_bytes() for p in source.iterdir()} == before


def test_cli_strict_failure_report_only_and_permissive_explicit(tmp_path):
    source = directory(tmp_path)
    with (source/"application.jsonl").open("a") as f: f.write("{broken\n")
    out = tmp_path/"strict"
    assert main([str(source),"--output-dir",str(out)]) == 2
    assert not (out/"records.jsonl").exists()
    report = json.loads((out/"ingestion-report.json").read_text())
    assert not report["complete"] and report["errors"][0]["line_number"] == 2
    permissive = tmp_path/"permissive"
    assert main([str(source),"--output-dir",str(permissive),"--permissive"]) == 2
    assert len((permissive/"records.jsonl").read_text().splitlines()) == 1
    assert not json.loads((permissive/"ingestion-report.json").read_text())["complete"]


def test_cli_prevents_source_writes_and_symlink_outputs(tmp_path):
    source = directory(tmp_path)
    before = {p.name:p.read_bytes() for p in source.iterdir()}
    assert main([str(source),"--output-dir",str(source/"new")]) == 1
    assert not (source/"new").exists()
    out=tmp_path/"output"
    out.mkdir()
    (out/"records.jsonl").symlink_to(source/"application.jsonl")
    assert main([str(source),"--output-dir",str(out),"--overwrite"]) == 1
    alias=tmp_path/"output-alias"
    alias.symlink_to(out, target_is_directory=True)
    assert main([str(source),"--output-dir",str(alias),"--overwrite"]) == 1
    assert {p.name:p.read_bytes() for p in source.iterdir()} == before
