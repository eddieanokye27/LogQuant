"""Strict JSON, exact retained-citation membership, and curated evidence limitations."""
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from typing import TypeVar

from pydantic import BaseModel
from logquant.evidence.contracts import BundleAudit, Citation, EvidencePayload
from logquant.evidence.selection import payload_bytes
from logquant.ingestion.models import SOURCE_NAMES
from .contracts import EvidenceLimitations, RCAResponse, TriageResponse


@dataclass(frozen=True)
class AgentEvidence:
    payload: EvidencePayload
    limitations: EvidenceLimitations


def prepare_evidence(payload: EvidencePayload, audit: BundleAudit) -> AgentEvidence:
    payload = EvidencePayload.model_validate_json(payload.model_dump_json())
    audit = BundleAudit.model_validate_json(audit.model_dump_json())
    candidate = audit.candidate
    for value in (payload.requested_start, payload.requested_end, payload.candidate_start,
                  payload.candidate_end, payload.first_alert_at):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Evidence interval timestamps must be UTC aware")
    if (audit.status != "selected" or audit.reason is not None or not payload.evidence
            or (candidate.run_id, candidate.candidate_id, candidate.start, candidate.end, candidate.first_alert_at)
            != (payload.run_id, payload.candidate_id, payload.candidate_start, payload.candidate_end, payload.first_alert_at)
            or (audit.requested_start, audit.requested_end) != (payload.requested_start, payload.requested_end)):
        raise ValueError("Evidence payload/audit identity mismatch or unavailable candidate")
    if not (payload.requested_start <= payload.candidate_start < payload.candidate_end <= payload.requested_end):
        raise ValueError("Invalid evidence intervals")
    if not payload.candidate_start < payload.first_alert_at <= payload.candidate_end:
        raise ValueError("Candidate first-alert time lies outside its interval")
    if max(payload.candidate_start-payload.requested_start, payload.requested_end-payload.candidate_end) > timedelta(seconds=60):
        raise ValueError("Evidence context exceeds the existing 60-second cap")
    keys, ids, regions = [], set(), Counter()
    for item in payload.evidence:
        record = item.record
        identity = (item.citation.run_id, item.citation.line_id)
        if identity in ids or item.citation.run_id != payload.run_id or item.citation.line_id != record.line_id or record.line_id != f"{record.source}:{record.line_number}":
            raise ValueError("Invalid or duplicate retained citation identity")
        if not payload.requested_start <= record.timestamp < payload.requested_end:
            raise ValueError("Retained record lies outside requested evidence")
        region = "before" if record.timestamp < payload.candidate_start else "candidate" if record.timestamp < payload.candidate_end else "after"
        if item.region != region:
            raise ValueError("Evidence region mismatch")
        if region == "candidate" and record.line_id not in candidate.line_ids:
            raise ValueError("Retained candidate citation is absent from its candidate audit")
        ids.add(identity); regions[region] += 1
        keys.append((record.timestamp, SOURCE_NAMES.index(record.source), record.line_number))
    if keys != sorted(keys) or not regions["candidate"]:
        raise ValueError("Evidence must be chronological and retain candidate records")
    encoded = payload_bytes(payload)
    if audit.payload_bytes != len(encoded) or audit.payload_sha256 != hashlib.sha256(encoded).hexdigest():
        raise ValueError("Evidence payload checksum/size mismatch")
    if audit.retained_citations != tuple(e.citation for e in payload.evidence):
        raise ValueError("Audit retained citations differ from payload")
    for region in ("before", "candidate", "after"):
        available, retained, omitted = (getattr(getattr(audit, name), region) for name in ("available", "retained", "omitted"))
        if retained != regions[region] or min(available, retained, omitted) < 0 or available != retained+omitted:
            raise ValueError("Audit omission counts are inconsistent")
    if audit.candidate_evidence_omitted != (audit.omitted.candidate > 0):
        raise ValueError("Candidate omission flag is inconsistent")
    limitations = EvidenceLimitations(omitted=audit.omitted, candidate_evidence_omitted=audit.candidate_evidence_omitted,
        before_observed_extent_shortfall_seconds=audit.before_observed_extent_shortfall_seconds,
        after_observed_extent_shortfall_seconds=audit.after_observed_extent_shortfall_seconds)
    return AgentEvidence(payload, limitations)


def _pairs(pairs: list[tuple]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError("Duplicate response JSON key")
        value[key] = item
    return value


def _nonfinite(value: str):
    raise ValueError("Non-finite response JSON number")


T = TypeVar("T", TriageResponse, RCAResponse)


def validate_response(raw: str, schema: type[T], evidence: AgentEvidence) -> T:
    # No fence stripping, rank sorting, category aliases or fallback status coercion.
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    response = schema.model_validate_json(json.dumps(value))
    lookup = {(e.citation.run_id, e.citation.line_id): e.record for e in evidence.payload.evidence}
    def visit(item, path: str) -> None:
        if isinstance(item, Citation):
            if (item.run_id, item.line_id) not in lookup:
                raise ValueError(f"{path}: citation is not retained in this exact run/bundle")
        elif isinstance(item, BaseModel):
            for field in type(item).model_fields: visit(getattr(item, field), f"{path}.{field}")
        elif isinstance(item, tuple):
            for index, child in enumerate(item): visit(child, f"{path}[{index}]")
    visit(response, schema.__name__)
    if isinstance(response, TriageResponse):
        for event in response.timeline:
            if event.timestamp not in {lookup[(c.run_id, c.line_id)].timestamp for c in event.citations}:
                raise ValueError("Timeline timestamp does not match its cited records")
        for service in response.affected_services:
            if service.service not in {lookup[(c.run_id, c.line_id)].service for c in service.citations}:
                raise ValueError("Affected service does not match its cited records")
    return response
