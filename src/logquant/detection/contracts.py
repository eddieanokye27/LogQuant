"""Detector configuration, provenance and score contracts; no alert decisions."""
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from logquant.ingestion.models import Contract


class DetectorConfig(Contract):
    seed: int = Field(default=42, ge=0, le=2**32-1)
    n_estimators: int = Field(default=100, ge=1, le=1000)
    max_samples: int = Field(default=256, ge=2, le=1_000_000)
    max_features: int = Field(default=5000, ge=1, le=100_000)
    ngram_min: int = Field(default=1, ge=1, le=3)
    ngram_max: int = Field(default=2, ge=1, le=3)
    edge_policy: Literal["exclude-fit-score-with-coverage-flag"] = "exclude-fit-score-with-coverage-flag"

    @model_validator(mode="after")
    def ordered_ngrams(self) -> "DetectorConfig":
        if self.ngram_min > self.ngram_max:
            raise ValueError("ngram_min must not exceed ngram_max")
        return self


class TrainingProvenance(Contract):
    run_id: str
    slot_id: str
    split: Literal["train"] = "train"
    valid: Literal[True] = True
    designation: Literal["healthy"] = "healthy"
    diagnostic_directory: str
    source_sha256: dict[str, str]
    verification_sha256: str
    assignment_sha256: str
    feature_run_sha256: str


class TrainingWindow(Contract):
    run_id: str
    window_id: str
    document_sha256: str
    status: Literal["fit", "excluded_edge", "empty_document", "zero_vector"]


class ModelMetadata(Contract):
    schema_version: Literal[1] = 1
    model_id: str
    usage: Literal["synthetic-test-only", "research"]
    config: DetectorConfig
    normalization_version: Literal["feature-v1"] = "feature-v1"
    windowing_version: Literal["epoch-utc-30s-v1"] = "epoch-utc-30s-v1"
    score_definition: Literal["negative-score_samples; higher-is-more-anomalous"] = "negative-score_samples; higher-is-more-anomalous"
    alert_threshold: None = None
    selection_manifest_sha256: str
    training_provenance: tuple[TrainingProvenance, ...]
    training_windows: tuple[TrainingWindow, ...]
    vectorizer_fit_windows: int
    detector_fit_windows: int
    effective_max_samples: int
    vocabulary_size: int
    fixed_parameters: dict[str, str]
    dependency_versions: dict[str, str]
    analysis_lock_sha256: str
    implementation_sha256: str


class WindowScore(Contract):
    run_id: str
    window_id: str
    start: datetime
    end: datetime
    line_ids: tuple[str, ...]
    status: Literal["scored", "empty_document", "zero_vector"]
    anomaly_score: float | None
    coverage_unknown: bool


class ScoringResult(Contract):
    model_id: str
    model_usage: Literal["synthetic-test-only", "research"]
    score_definition: str
    alert_threshold: None = None
    input_sha256: dict[str, str]
    scores: tuple[WindowScore, ...]
