"""Original diagnostic evidence payloads and separate selection audit contracts."""
from datetime import datetime
from typing import Literal

from pydantic import Field
from logquant.ingestion.models import Contract, LogRecord
from logquant.thresholds.contracts import Candidate

POLICY = "candidate-first-alternating-nearest-context-v1"


class EvidenceConfig(Contract):
    before_seconds: int = Field(default=60, ge=0, le=60)
    after_seconds: int = Field(default=60, ge=0, le=60)
    max_records: int = Field(default=200, ge=1)
    max_payload_bytes: int = Field(default=262144, ge=1)


class Citation(Contract):
    run_id: str
    line_id: str


class EvidenceRecord(Contract):
    citation: Citation
    region: Literal["before", "candidate", "after"]
    record: LogRecord


class EvidencePayload(Contract):
    schema_version: Literal[1] = 1
    mode: Literal["offline-with-context"] = "offline-with-context"
    run_id: str
    candidate_id: str
    candidate_start: datetime
    candidate_end: datetime
    first_alert_at: datetime
    requested_start: datetime
    requested_end: datetime
    evidence: tuple[EvidenceRecord, ...]


class RegionCounts(Contract):
    before: int
    candidate: int
    after: int


class BundleAudit(Contract):
    candidate: Candidate
    status: Literal["selected", "budget_too_small"]
    reason: str | None
    requested_start: datetime
    requested_end: datetime
    available: RegionCounts
    retained: RegionCounts
    omitted: RegionCounts
    oversized_individual: RegionCounts
    candidate_evidence_omitted: bool
    before_observed_extent_shortfall_seconds: float
    after_observed_extent_shortfall_seconds: float
    retained_citations: tuple[Citation, ...]
    empty_payload_bytes: int
    smallest_candidate_payload_bytes: int
    payload_bytes: int | None
    payload_sha256: str | None


class EvidenceReport(Contract):
    schema_version: Literal[1] = 1
    policy: Literal[POLICY] = POLICY
    run_id: str
    input_usage: Literal["synthetic-test-only", "research"]
    status: Literal["selected", "no_candidates", "budget_failure"]
    config: EvidenceConfig
    diagnostic_sha256: dict[str, str]
    alert_input_sha256: dict[str, str]
    observed_first_event: datetime
    observed_last_event: datetime
    bundles: tuple[BundleAudit, ...]


class EvidenceResult(Contract):
    payloads: tuple[EvidencePayload, ...]
    report: EvidenceReport
