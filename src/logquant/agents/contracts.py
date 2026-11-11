"""Provider-independent strict Triage/RCA and execution-budget contracts."""
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator
from logquant.ingestion.models import Contract
from logquant.evidence.contracts import Citation, RegionCounts

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Citations = Annotated[tuple[Citation, ...], Field(min_length=1)]


class Category(str, Enum):
    DATABASE_UNAVAILABLE = "Database unavailable"
    INVALID_CREDENTIALS = "Invalid database credentials"
    APPLICATION_EXCEPTION = "Application exception"
    PERMISSION_DENIED = "Database permission denied"
    LOCK_CONTENTION = "Database lock contention"


CATALOGUE = (
    (Category.DATABASE_UNAVAILABLE, "The application cannot reach or establish a working PostgreSQL connection; distinguish connectivity from authentication and privilege errors."),
    (Category.INVALID_CREDENTIALS, "PostgreSQL authentication fails for the application's supplied credentials."),
    (Category.APPLICATION_EXCEPTION, "An application runtime exception disrupts an ordinary operation; distinguish it from a database failure."),
    (Category.PERMISSION_DENIED, "An authenticated application operation lacks required PostgreSQL privileges."),
    (Category.LOCK_CONTENTION, "Conflicting database locks cause observed blocking or lock-wait diagnostics; a generic timeout alone is insufficient."),
)


class Observation(Contract):
    text: Text
    citations: Citations


class AffectedService(Contract):
    service: Literal["application", "postgresql"]
    description: Text
    citations: Citations


class TimelineEvent(Contract):
    timestamp: datetime
    description: Text
    citations: Citations

    @field_validator("timestamp")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timeline timestamps require an explicit timezone")
        return value.astimezone(timezone.utc)


class TriageResponse(Contract):
    schema_version: Literal[1] = 1
    symptoms: tuple[Observation, ...]
    affected_services: tuple[AffectedService, ...]
    timeline: tuple[TimelineEvent, ...]
    uncertainties: tuple[Text, ...] = Field(min_length=1)
    evidence_limitations: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> "TriageResponse":
        if any(a.timestamp > b.timestamp for a, b in zip(self.timeline, self.timeline[1:])):
            raise ValueError("Timeline must be chronological")
        if len({s.service for s in self.affected_services}) != len(self.affected_services):
            raise ValueError("Affected services must be distinct")
        return self


class Hypothesis(Contract):
    rank: int = Field(ge=1, le=2)
    category: Category
    probable_component: Literal["application", "postgresql", "unknown"]
    explanation: Text  # Causal inference, separate from directly cited observations.
    citations: Citations
    mitigations: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def leading_mitigation(self) -> "Hypothesis":
        if self.rank == 1 and not self.mitigations:
            raise ValueError("The leading hypothesis requires mitigation suggestions")
        return self


class RCAResponse(Contract):
    schema_version: Literal[1] = 1
    status: Literal["diagnosis", "no_incident", "insufficient_evidence"]
    summary: Text
    observations: tuple[Observation, ...]
    hypotheses: tuple[Hypothesis, ...] = Field(max_length=2)
    uncertainties: tuple[Text, ...] = Field(min_length=1)
    evidence_limitations: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def status_and_ranks(self) -> "RCAResponse":
        if self.status == "diagnosis":
            if not self.hypotheses or not self.observations:
                raise ValueError("Diagnosis requires hypotheses and cited observations")
            if [h.rank for h in self.hypotheses] != list(range(1, len(self.hypotheses)+1)):
                raise ValueError("Hypotheses must be ordered ranks 1, then optional 2")
            if len({h.category for h in self.hypotheses}) != len(self.hypotheses):
                raise ValueError("Hypothesis categories must be distinct")
        elif self.hypotheses:
            raise ValueError("Non-diagnosis statuses require zero hypotheses")
        if self.status == "no_incident" and not self.observations:
            raise ValueError("No-incident claims require cited observations")
        return self


class EvidenceLimitations(Contract):
    omitted: RegionCounts
    candidate_evidence_omitted: bool
    before_observed_extent_shortfall_seconds: float = Field(ge=0, le=60, allow_inf_nan=False)
    after_observed_extent_shortfall_seconds: float = Field(ge=0, le=60, allow_inf_nan=False)
    coverage_note: Literal["Bounded offline observations do not establish complete collection coverage."] = "Bounded offline observations do not establish complete collection coverage."


class ExecutionBudget(Contract):
    context_limit_tokens: int = Field(default=16384, ge=1)
    max_input_tokens: int = Field(default=12000, ge=1)
    triage_output_tokens: int = Field(default=1000, ge=1)
    rca_output_tokens: int = Field(default=1800, ge=1)
    triage_rca_reserve_tokens: int = Field(default=2000, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    max_repair_attempts: int = Field(default=1, ge=0, le=1)
    transport_retries: Literal[0] = 0

    @model_validator(mode="after")
    def compatible(self) -> "ExecutionBudget":
        if self.max_input_tokens + max(self.triage_output_tokens, self.rca_output_tokens) > self.context_limit_tokens:
            raise ValueError("Configured input/output caps exceed context capacity")
        if self.triage_rca_reserve_tokens < self.triage_output_tokens:
            raise ValueError("RCA must reserve at least the Triage output allowance")
        return self
