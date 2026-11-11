"""Deterministic bounded original evidence. No evaluation or model loading."""
from collections import Counter
from datetime import timedelta
import hashlib
import json

from logquant.ingestion.models import IngestionResult, LogRecord, SOURCE_NAMES
from logquant.detection.contracts import ScoringResult
from logquant.features.windowing import ANCHOR, WIDTH
from logquant.thresholds.alerts import classify
from logquant.thresholds.contracts import AlertResult
from .contracts import (BundleAudit, Citation, EvidenceConfig, EvidencePayload, EvidenceRecord,
                        EvidenceReport, EvidenceResult, RegionCounts)


def payload_bytes(payload: EvidencePayload) -> bytes:
    """The exact agent-facing serialized payload, including its terminal newline."""
    return (json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)+"\n").encode("utf-8")


def _key(record: LogRecord) -> tuple:
    return record.timestamp, SOURCE_NAMES.index(record.source), record.line_number


def _counts(items: list[EvidenceRecord]) -> RegionCounts:
    counts = Counter(item.region for item in items)
    return RegionCounts(before=counts["before"], candidate=counts["candidate"], after=counts["after"])


def _verify(result: IngestionResult, run_id: str, alerts: AlertResult,
            diagnostic_sha256: dict[str, str]) -> tuple[tuple[LogRecord, ...], AlertResult]:
    if not run_id.strip() or not result.complete or result.report.errors or not result.records:
        raise ValueError("Evidence requires explicit run identity and complete nonempty ingestion")
    if result.report.record_count != len(result.records):
        raise ValueError("Ingestion record count mismatch")
    sources = result.report.sources
    if (len(sources) != 2 or {s.source for s in sources} != set(SOURCE_NAMES)
            or any(s.state != "read" or s.sha256 is None for s in sources)):
        raise ValueError("Evidence requires both complete diagnostic input files")
    if {s.source: s.sha256 for s in sources} != diagnostic_sha256:
        raise ValueError("Alert diagnostic fingerprints differ from supplied ingestion")
    records = tuple(sorted(result.records, key=_key))
    if len({r.line_id for r in records}) != len(records) or any(
            r.line_id != f"{r.source}:{r.line_number}" for r in records):
        raise ValueError("Invalid or duplicate physical evidence identity")
    if any(s.valid_records != sum(r.source == s.source for r in records) for s in sources):
        raise ValueError("Per-source record count mismatch")
    alerts = AlertResult.model_validate_json(alerts.model_dump_json())
    if alerts.coverage.run_id != run_id or any(w.score.run_id != run_id for w in alerts.windows):
        raise ValueError("Evidence cannot combine runs")
    raw = ScoringResult(model_id=alerts.detector.model_id, model_usage=alerts.detector.usage,
        score_definition=alerts.score_definition, input_sha256=diagnostic_sha256,
        scores=tuple(w.score for w in alerts.windows))
    expected = classify(raw, alerts.detector, alerts.threshold)
    if (tuple(sorted(alerts.windows, key=lambda w: w.score.start)) != expected.windows
            or tuple(sorted(alerts.candidates, key=lambda c: c.start)) != expected.candidates
            or alerts.coverage != expected.coverage):
        raise ValueError("Supplied alerts/candidates differ from unchanged grouping policy")
    first = ANCHOR+((records[0].timestamp-ANCHOR)//WIDTH)*WIDTH
    last = ANCHOR+((records[-1].timestamp-ANCHOR)//WIDTH)*WIDTH
    if expected.windows[0].score.start != first or expected.windows[-1].score.start != last:
        raise ValueError("Alert grid does not cover supplied observed record buckets")
    for window in expected.windows:
        inside = tuple(r.line_id for r in records if window.score.start <= r.timestamp < window.score.end)
        if window.score.line_ids != inside or (window.score.status == "empty_document") != (not inside):
            raise ValueError("Window evidence references do not match original records")
    return records, expected


def select_evidence(result: IngestionResult, *, run_id: str, alerts: AlertResult,
                    diagnostic_sha256: dict[str, str], config: EvidenceConfig | None = None,
                    alert_input_sha256: dict[str, str] | None = None) -> EvidenceResult:
    config = EvidenceConfig() if config is None else EvidenceConfig.model_validate_json(config.model_dump_json())
    records, alerts = _verify(result, run_id, alerts, diagnostic_sha256)
    payloads, audits = [], []
    for candidate in alerts.candidates:
        try:
            start = candidate.start-timedelta(seconds=config.before_seconds)
            end = candidate.end+timedelta(seconds=config.after_seconds)
        except OverflowError as exc:
            raise ValueError("Requested context cannot be represented by UTC datetimes") from exc
        regions: dict[str, list[EvidenceRecord]] = {"before": [], "candidate": [], "after": []}
        for record in records:
            if start <= record.timestamp < end:
                region = "before" if record.timestamp < candidate.start else "candidate" if record.timestamp < candidate.end else "after"
                regions[region].append(EvidenceRecord(citation=Citation(run_id=run_id, line_id=record.line_id),
                                                      region=region, record=record))
        if not regions["candidate"]:
            raise ValueError("Candidate has no original diagnostic evidence")
        header = dict(run_id=run_id, candidate_id=candidate.candidate_id, candidate_start=candidate.start,
            candidate_end=candidate.end, first_alert_at=candidate.first_alert_at, requested_start=start, requested_end=end)
        def make(items: list[EvidenceRecord]) -> EvidencePayload:
            return EvidencePayload(**header, evidence=tuple(sorted(items, key=lambda item: _key(item.record))))
        all_items = regions["before"]+regions["candidate"]+regions["after"]
        single_sizes = {item.citation.line_id: len(payload_bytes(make([item]))) for item in all_items}
        oversized = [item for item in all_items if single_sizes[item.citation.line_id] > config.max_payload_bytes]
        minimum = min(single_sizes[item.citation.line_id] for item in regions["candidate"])
        kept: list[EvidenceRecord] = []
        reason = None
        if minimum > config.max_payload_bytes:
            reason = "No complete candidate record fits the serialized payload byte budget"
        else:
            before = sorted(regions["before"], key=lambda item: (candidate.start-item.record.timestamp,
                            SOURCE_NAMES.index(item.record.source), item.record.line_number))
            after = regions["after"]
            context: list[EvidenceRecord] = []
            for index in range(max(len(before), len(after))):
                for side in (before, after):
                    if index < len(side):
                        context.append(side[index])
            for item in regions["candidate"]+context:
                if len(kept) < config.max_records and len(payload_bytes(make([*kept, item]))) <= config.max_payload_bytes:
                    kept.append(item)
        payload = make(kept) if kept else None
        retained = _counts(kept)
        available = _counts(all_items)
        omitted = RegionCounts(**{region: getattr(available, region)-getattr(retained, region) for region in regions})
        encoded = payload_bytes(payload) if payload is not None else None
        if payload is not None:
            assert len(payload.evidence) <= config.max_records and len(encoded) <= config.max_payload_bytes
            payloads.append(payload)
        audits.append(BundleAudit(candidate=candidate, status="selected" if payload is not None else "budget_too_small",
            reason=reason, requested_start=start, requested_end=end, available=available, retained=retained,
            omitted=omitted, oversized_individual=_counts(oversized), candidate_evidence_omitted=omitted.candidate > 0,
            before_observed_extent_shortfall_seconds=min(float(config.before_seconds), max(0.0, (records[0].timestamp-start).total_seconds())),
            after_observed_extent_shortfall_seconds=min(float(config.after_seconds), max(0.0, (end-records[-1].timestamp).total_seconds())),
            retained_citations=tuple(item.citation for item in payload.evidence) if payload else (),
            empty_payload_bytes=len(payload_bytes(make([]))), smallest_candidate_payload_bytes=minimum,
            payload_bytes=len(encoded) if encoded is not None else None,
            payload_sha256=hashlib.sha256(encoded).hexdigest() if encoded is not None else None))
    return EvidenceResult(payloads=tuple(payloads), report=EvidenceReport(run_id=run_id,
        input_usage=alerts.detector.usage, status="no_candidates" if not audits else "budget_failure" if any(
            a.status == "budget_too_small" for a in audits) else "selected", config=config,
        diagnostic_sha256=diagnostic_sha256, alert_input_sha256=alert_input_sha256 or {},
        observed_first_event=records[0].timestamp, observed_last_event=records[-1].timestamp, bundles=tuple(audits)))
