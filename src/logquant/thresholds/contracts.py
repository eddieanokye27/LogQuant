"""Versioned calibration artifacts and diagnostic-only alert output contracts."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator
from logquant.ingestion.models import Contract
from logquant.detection.contracts import WindowScore

SCORE = "negative-score_samples; higher-is-more-anomalous"
POLICY = "dev-recall-budget-v1;gt;adjacent-v1;interior-scored-v1;first-window-end-v1"


class CalibrationConfig(Contract):
    max_false_candidates_per_healthy_hour: float = Field(ge=0, allow_inf_nan=False)
    minimum_faulty_run_recall: float = Field(gt=0, le=1, allow_inf_nan=False)


class DetectorIdentity(Contract):
    model_id: str
    usage: Literal["synthetic-test-only", "research"]
    payload_sha256: str


class FailureInterval(Contract):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Failure interval requires timezone-aware timestamps")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def ordered(self) -> "FailureInterval":
        if self.start >= self.end:
            raise ValueError("Failure interval must have positive duration")
        return self


class AlertWindow(Contract):
    score: WindowScore
    state: Literal["alert", "below_threshold", "unscored", "excluded_edge"]


class Candidate(Contract):
    candidate_id: str
    run_id: str
    start: datetime
    end: datetime
    first_alert_at: datetime
    window_ids: tuple[str, ...]
    line_ids: tuple[str, ...]


class Coverage(Contract):
    run_id: str
    interior_seconds: int
    scorable_seconds: int
    unscored_seconds: int
    excluded_edge_windows: int
    unscored_windows: int


class AlertResult(Contract):
    detector: DetectorIdentity
    threshold: float = Field(allow_inf_nan=False)
    policy: Literal[POLICY] = POLICY
    score_definition: Literal[SCORE] = SCORE
    windows: tuple[AlertWindow, ...]
    candidates: tuple[Candidate, ...]
    coverage: Coverage


class CalibrationInput(Contract):
    run_id: str
    directory: str
    score_directory: str
    hashes: dict[str, str]


class ThresholdTrial(Contract):
    threshold: float = Field(allow_inf_nan=False)
    detected_faulty_runs: tuple[str, ...]
    overlapping_faulty_runs: tuple[str, ...]
    missed_faulty_runs: tuple[str, ...]
    faulty_run_count: int
    recall: float
    healthy_candidates: int
    healthy_scorable_seconds: int
    false_candidates_per_healthy_hour: float | None
    feasible: bool


class CalibrationArtifact(Contract):
    schema_version: Literal[1] = 1
    artifact_id: str
    status: Literal["selected", "unsuccessful"]
    reasons: tuple[str, ...]
    detector: DetectorIdentity
    policy: Literal[POLICY] = POLICY
    score_definition: Literal[SCORE] = SCORE
    config: CalibrationConfig
    selection_sha256: str
    inputs: tuple[CalibrationInput, ...]
    coverage: tuple[Coverage, ...]
    trials: tuple[ThresholdTrial, ...]
    threshold: float | None = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def outcome(self) -> "CalibrationArtifact":
        if (self.status == "selected") != (self.threshold is not None):
            raise ValueError("Only successful calibration has a threshold")
        if self.status == "selected" and (self.reasons or not any(
                t.threshold == self.threshold and t.feasible for t in self.trials)):
            raise ValueError("Selected threshold must match a feasible trial")
        if self.status == "unsuccessful" and not self.reasons:
            raise ValueError("Unsuccessful calibration needs reasons")
        return self
