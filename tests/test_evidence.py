from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import runpy
from unittest.mock import patch

import pytest

from logquant.evidence import EvidenceConfig, payload_bytes, select_evidence
from logquant.evidence.cli import load_alert_export, main
from logquant.ingestion import load_diagnostic_directory

CREATE = runpy.run_path(str(Path(__file__).resolve().parents[1]/"scripts/make_synthetic_evidence_example.py"))["create_example"]
RUN = "synthetic-evidence-01"


@pytest.fixture
def example(tmp_path):
    source = CREATE(tmp_path/"test-only")
    return source


def select(source, config=None, alerts_name="alerts", ingestion=None, alerts=None, hashes=None):
    result = load_diagnostic_directory(source) if ingestion is None else ingestion
    loaded, diagnostic_hashes, alert_hashes = load_alert_export(source.parent/alerts_name)
    return select_evidence(result, run_id=RUN, alerts=loaded if alerts is None else alerts,
        diagnostic_sha256=diagnostic_hashes if hashes is None else hashes,
        config=config, alert_input_sha256=alert_hashes)


def test_half_open_context_boundaries_and_original_diagnostic_evidence(example):
    original = load_diagnostic_directory(example)
    snapshot = original.model_dump_json()
    result = select(example, ingestion=original)
    payload, = result.payloads
    audit, = result.report.bundles
    assert (audit.available.before, audit.available.candidate, audit.available.after) == (4, 7, 4)
    assert audit.omitted.model_dump() == dict(before=0, candidate=0, after=0)
    assert payload.evidence[0].record.timestamp == payload.requested_start
    assert all(item.record.timestamp < payload.requested_end for item in payload.evidence)
    exact_start = [r for r in payload.evidence if r.record.timestamp == payload.candidate_start]
    assert [r.record.line_id for r in exact_start] == ["application.jsonl:5", "postgresql.jsonl:1", "postgresql.jsonl:2"]
    assert all(r.region == "candidate" for r in exact_start)
    assert next(r for r in payload.evidence if r.record.timestamp == payload.candidate_end).region == "after"
    lookup = {r.line_id: r for r in original.records}
    for item in payload.evidence:
        assert item.citation.run_id == RUN and item.citation.line_id == item.record.line_id
        assert item.record.model_dump_json() == lookup[item.citation.line_id].model_dump_json()
    error = next(e.record for e in payload.evidence if e.record.exception_type)
    assert error.exception_type == "LockNotAvailable" and "café" in error.exception_message
    assert error.request_id == "opaque-request-4" and error.route == "/products/123/quote"
    assert error.raw_text.startswith("  ") and error.raw_text.endswith(" \r\n")
    assert {e.record.sqlstate for e in payload.evidence} >= {"55P03", "42501"}
    duplicates = [e for e in payload.evidence if e.record.sqlstate == "55P03"]
    assert duplicates[0].record.raw_record == duplicates[1].record.raw_record
    assert duplicates[0].citation != duplicates[1].citation
    assert original.model_dump_json() == snapshot


def test_context_disabled_and_custom_boundaries(example):
    none = select(example, EvidenceConfig(before_seconds=0, after_seconds=0))
    assert {r.region for r in none.payloads[0].evidence} == {"candidate"}
    one = select(example, EvidenceConfig(before_seconds=30, after_seconds=30))
    assert one.report.bundles[0].available.before == 2 and one.report.bundles[0].available.after == 2


@pytest.mark.parametrize("changes", [{"before_seconds":61}, {"after_seconds":-1}, {"before_seconds":True},
    {"max_records":0}, {"max_payload_bytes":0}, {"token_limit":100}, {"after_seconds":1.5}])
def test_invalid_config(changes):
    with pytest.raises(ValueError): EvidenceConfig(**changes)


def test_record_budget_prioritizes_candidate_and_both_context_sides(example):
    result = select(example, EvidenceConfig(max_records=9))
    payload = result.payloads[0]; audit = result.report.bundles[0]
    assert audit.retained.model_dump() == dict(before=1, candidate=7, after=1)
    assert audit.omitted.model_dump() == dict(before=3, candidate=0, after=3)
    assert not audit.candidate_evidence_omitted
    assert next(e.record.line_id for e in payload.evidence if e.region == "before") == "application.jsonl:4"
    assert next(e.record.line_id for e in payload.evidence if e.region == "after") == "application.jsonl:9"
    limited = select(example, EvidenceConfig(max_records=2))
    assert limited.report.bundles[0].candidate_evidence_omitted
    assert all(e.region == "candidate" for e in limited.payloads[0].evidence)


def test_serialized_utf8_budget_counts_metadata_escaping_newline_and_exact_limit(example):
    all_records = select(example)
    payload = all_records.payloads[0]; exact = len(payload_bytes(payload))
    assert exact == all_records.report.bundles[0].payload_bytes
    assert hashlib.sha256(payload_bytes(payload)).hexdigest() == all_records.report.bundles[0].payload_sha256
    assert payload_bytes(payload).endswith(b"\n") and b"caf\xc3\xa9" in payload_bytes(payload)
    assert select(example, EvidenceConfig(max_payload_bytes=exact)).payloads == all_records.payloads
    smaller = select(example, EvidenceConfig(max_payload_bytes=exact-1))
    assert len(smaller.payloads[0].evidence) < len(payload.evidence)
    assert len(payload_bytes(smaller.payloads[0])) <= exact-1
    assert json.loads(payload_bytes(payload))["evidence"][0]["record"]["raw_text"] == payload.evidence[0].record.raw_text


def test_oversized_individual_skipped_whole_and_later_candidate_retained(example):
    path = example/"application.jsonl"
    rows = [json.loads(s) for s in path.read_text().splitlines()]
    rows[4]["message"] = "unbroken-"+"🧪"*10000
    path.write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in rows))
    # Fingerprints must explicitly match this test's modified original diagnostics.
    ingestion = load_diagnostic_directory(example)
    hashes = {s.source:s.sha256 for s in ingestion.report.sources}
    original_text = ingestion.records[4].raw_text
    result = select(example, EvidenceConfig(max_payload_bytes=7000), ingestion=ingestion, hashes=hashes)
    audit = result.report.bundles[0]
    assert result.payloads and audit.oversized_individual.candidate >= 1 and audit.candidate_evidence_omitted
    assert all(e.citation.line_id != "application.jsonl:5" for e in result.payloads[0].evidence)
    assert any(e.region == "candidate" for e in result.payloads[0].evidence)
    assert "🧪"*10000 in path.read_text() and original_text == ingestion.records[4].raw_text
    assert len(payload_bytes(result.payloads[0])) <= 7000


def test_too_small_returns_explicit_audit_without_invalid_payload(example):
    result = select(example, EvidenceConfig(max_payload_bytes=1))
    assert not result.payloads and result.report.status == "budget_failure"
    audit = result.report.bundles[0]
    assert audit.status == "budget_too_small" and audit.reason
    assert audit.omitted == audit.available and audit.candidate_evidence_omitted
    assert audit.payload_bytes is None and audit.smallest_candidate_payload_bytes > 1
    assert audit.retained_citations == () and audit.oversized_individual == audit.available


def test_no_candidates_does_not_create_incident(example):
    result = select(example, alerts_name="no-candidate-alerts")
    assert not result.payloads and not result.report.bundles and result.report.status == "no_candidates"


def test_ordering_is_deterministic_for_reordered_supplied_records_and_candidates(example):
    original = load_diagnostic_directory(example)
    alerts, hashes, _ = load_alert_export(example.parent/"two-candidate-alerts")
    expected = select(example, ingestion=original, alerts=alerts)
    reordered = original.model_copy(update={"records":tuple(reversed(original.records))})
    reversed_alerts = alerts.model_copy(update={"windows":tuple(reversed(alerts.windows)),
                                               "candidates":tuple(reversed(alerts.candidates))})
    actual = select(example, ingestion=reordered, alerts=reversed_alerts, hashes=hashes)
    assert actual == expected


def test_other_run_content_with_same_physical_ids_cannot_substitute(example, tmp_path):
    other = CREATE(tmp_path/"another-run")
    p = other/"application.jsonl"
    rows = [json.loads(s) for s in p.read_text().splitlines()]
    rows[4]["message"] = "other run content with reused physical IDs"
    p.write_text("".join(json.dumps(r)+"\n" for r in rows))
    other_records = load_diagnostic_directory(other)
    with pytest.raises(ValueError, match="fingerprints"):
        select(example, ingestion=other_records)


def test_multiple_bundles_keep_candidate_order_and_independent_budgets(example):
    result = select(example, EvidenceConfig(max_records=9), alerts_name="two-candidate-alerts")
    assert len(result.payloads) == 2
    assert result.payloads[0].candidate_start < result.payloads[1].candidate_start
    assert all(len(p.evidence) <= 9 for p in result.payloads)
    # Context may overlap across independent candidates; it is intentionally not deduplicated across bundles.
    assert {e.citation.line_id for e in result.payloads[0].evidence} & {e.citation.line_id for e in result.payloads[1].evidence}


def test_observation_shortfall_is_distinct_from_budget_omission(example):
    # Candidate starts at 60s; remove the preceding 0..30 bucket from the request by asking
    # for context around the later candidate, whose requested end exceeds the last event.
    result = select(example, alerts_name="two-candidate-alerts")
    later = result.report.bundles[1]
    assert later.after_observed_extent_shortfall_seconds == 30
    assert later.omitted.after == 0 and later.available.after > 0
    # A shortened context request has no extent shortfall but budget can omit known records.
    bounded = select(example, EvidenceConfig(max_records=1), alerts_name="two-candidate-alerts")
    assert bounded.report.bundles[0].before_observed_extent_shortfall_seconds == 0
    assert bounded.report.bundles[0].omitted.before == 4


@pytest.mark.parametrize("kind", ["wrong_run", "fingerprint", "candidate_lines", "candidate_windows", "candidate_end", "candidate_id", "first_alert", "window_lines", "missing_window", "missing_candidate", "state", "incomplete", "duplicate_line", "record_time"])
def test_reference_integrity_rejects_tampering(example, kind):
    ingestion = load_diagnostic_directory(example)
    alerts, hashes, _ = load_alert_export(example.parent/"alerts")
    if kind == "wrong_run": alerts = alerts.model_copy(update={"coverage":alerts.coverage.model_copy(update={"run_id":"another"})})
    elif kind == "fingerprint": hashes = {**hashes, "application.jsonl":"wrong"}
    elif kind.startswith("candidate_") or kind == "first_alert":
        c = alerts.candidates[0]
        field, value = {"candidate_lines":("line_ids",("application.jsonl:999",)),
            "candidate_windows":("window_ids",()), "candidate_end":("end", c.end+timedelta(seconds=30)),
            "candidate_id":("candidate_id","wrong"), "first_alert":("first_alert_at",c.start)}[kind]
        alerts = alerts.model_copy(update={"candidates":(c.model_copy(update={field:value}),)})
    elif kind == "window_lines":
        w = alerts.windows[2]; modified = w.model_copy(update={"score":w.score.model_copy(update={"line_ids":("application.jsonl:999",)})})
        alerts = alerts.model_copy(update={"windows":(*alerts.windows[:2],modified,*alerts.windows[3:])})
    elif kind == "missing_window": alerts = alerts.model_copy(update={"windows":alerts.windows[1:]})
    elif kind == "missing_candidate": alerts = alerts.model_copy(update={"candidates":()})
    elif kind == "state":
        w = alerts.windows[2].model_copy(update={"state":"below_threshold"})
        alerts = alerts.model_copy(update={"windows":(*alerts.windows[:2],w,*alerts.windows[3:])})
    elif kind == "incomplete": ingestion = ingestion.model_copy(update={"report":ingestion.report.model_copy(update={"complete":False})})
    elif kind == "duplicate_line": ingestion = ingestion.model_copy(update={"records":(ingestion.records[0], *ingestion.records[:-1])})
    elif kind == "record_time":
        r = ingestion.records[4].model_copy(update={"timestamp":ingestion.records[4].timestamp+timedelta(seconds=100)})
        ingestion = ingestion.model_copy(update={"records":(*ingestion.records[:4],r,*ingestion.records[5:])})
    with pytest.raises(ValueError): select(example, ingestion=ingestion, alerts=alerts, hashes=hashes)


def test_cli_determinism_allowlist_no_overwrite_and_source_unchanged(example, tmp_path):
    (example.parent/"evaluation").mkdir()
    (example.parent/"evaluation/ground_truth.json").write_text("MUST NEVER READ")
    snapshots = {p:p.read_bytes() for p in example.rglob("*") if p.is_file()}
    args = [str(example),"--run-id",RUN,"--alerts-dir",str(example.parent/"alerts")]
    with patch("logquant.features.windowing.normalize_record", side_effect=AssertionError("No ML text")), \
         patch("logquant.thresholds.orchestration.metadata", side_effect=AssertionError("No labels")):
        assert main([*args,"--output-dir",str(tmp_path/"first")]) == 0
        assert main([*args,"--output-dir",str(tmp_path/"second")]) == 0
    first = {p.name:p.read_bytes() for p in (tmp_path/"first").iterdir()}
    assert first == {p.name:p.read_bytes() for p in (tmp_path/"second").iterdir()}
    assert main([*args,"--output-dir",str(tmp_path/"first")]) == 1
    assert main([*args,"--output-dir",str(tmp_path/"first"),"--overwrite"]) == 0
    assert all(p.read_bytes() == data for p,data in snapshots.items())
    assert b"MUST NEVER READ" not in b"".join(first.values())
    assert main([*args,"--output-dir",str(example/"out")]) == 1


def test_cli_no_candidate_budget_failure_and_symlink_rejection(example,tmp_path):
    args = [str(example),"--run-id",RUN]
    assert main([*args,"--alerts-dir",str(example.parent/"no-candidate-alerts"),"--output-dir",str(tmp_path/"none")]) == 0
    assert (tmp_path/"none/evidence.jsonl").read_bytes() == b""
    assert main([*args,"--alerts-dir",str(example.parent/"alerts"),"--config",str(example.parent/"impossible-config.json"),"--output-dir",str(tmp_path/"tiny")]) == 3
    assert (tmp_path/"tiny/evidence.jsonl").read_bytes() == b""
    source = example.parent/"alerts/candidates.jsonl"
    actual = source.with_name("saved-candidates"); source.rename(actual); source.symlink_to(actual)
    assert main([*args,"--alerts-dir",str(example.parent/"alerts"),"--output-dir",str(tmp_path/"bad")]) == 1
    assert not (tmp_path/"bad").exists()
