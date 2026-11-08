"""Separate, versioned ML input representations; original evidence stays in ingestion."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator

from logquant.ingestion.models import Contract, IngestionReport, SourceName


class FeatureRecord(Contract):
    run_id: str
    line_id: str
    source: SourceName
    line_number: int = Field(ge=1)
    timestamp: datetime
    fields: dict[str, str]
    document: str
    changed_fields: tuple[str, ...]

    @field_validator("timestamp")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Feature timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)


class FeatureWindow(Contract):
    run_id: str
    window_id: str
    index: int
    start: datetime
    end: datetime
    line_ids: tuple[str, ...]
    document: str
    empty: bool
    first_observed_bucket: bool
    last_observed_bucket: bool


class FeatureRun(Contract):
    schema_version: Literal[1] = 1
    normalization_version: Literal["feature-v1"] = "feature-v1"
    windowing_version: Literal["epoch-utc-30s-v1"] = "epoch-utc-30s-v1"
    run_id: str
    window_seconds: Literal[30] = 30
    alignment_anchor: datetime
    boundary_basis: Literal["observed-events-only"] = "observed-events-only"
    observed_first_event: datetime | None
    observed_last_event: datetime | None
    features: tuple[FeatureRecord, ...]
    windows: tuple[FeatureWindow, ...]
    ingestion_report: IngestionReport
