"""Versioned ingestion contracts. Normalization is limited to the UTC timestamp."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

SourceName = Literal["application.jsonl", "postgresql.jsonl"]
SOURCE_NAMES: tuple[SourceName, ...] = ("application.jsonl", "postgresql.jsonl")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LogRecord(Contract):
    timestamp: datetime
    service: str
    severity: str
    message: str
    message_field: Literal["message", "event"]
    source: SourceName
    line_number: int = Field(ge=1)
    line_id: str
    raw_record: dict[str, JsonValue]
    raw_text: str
    event: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    request_id: str | None = None
    method: str | None = None
    route: str | None = None
    status: int | None = None
    duration_ms: float | None = None
    sqlstate: str | None = None
    detail: str | None = None
    hint: str | None = None
    context: str | None = None

    @field_validator("timestamp")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Common record timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)


class IngestionIssue(Contract):
    source: str
    line_number: int | None
    kind: Literal["boundary", "io", "encoding", "json", "schema", "timestamp", "timezone"]
    reason: str


class SourceReport(Contract):
    source: SourceName
    state: Literal["read", "missing", "rejected", "unreadable"]
    physical_lines: int = 0
    valid_records: int = 0
    bytes_read: int = 0
    sha256: str | None = None


class IngestionReport(Contract):
    schema_version: Literal[1] = 1
    parser_version: Literal["0.1.0"] = "0.1.0"
    mode: Literal["strict", "permissive"]
    complete: bool
    record_count: int
    sources: tuple[SourceReport, ...]
    errors: tuple[IngestionIssue, ...]


class IngestionResult(Contract):
    records: tuple[LogRecord, ...]
    report: IngestionReport

    @property
    def complete(self) -> bool:
        return self.report.complete


class IngestionError(ValueError):
    """Strict failure, with all discovered issues and partial records for inspection."""

    def __init__(self, result: IngestionResult):
        self.result = result
        first = result.report.errors[0]
        location = first.source + (f":{first.line_number}" if first.line_number else "")
        super().__init__(f"{location}: {first.reason} ({len(result.report.errors)} ingestion errors)")
