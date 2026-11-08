"""Source-specific JSONL adapters; no label loading, discovery or ML transforms."""
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Annotated, Literal, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .boundary import directory_fd, open_source
from .models import (SOURCE_NAMES, IngestionError, IngestionIssue, IngestionReport,
                     IngestionResult, LogRecord, SourceName, SourceReport)

Text = Annotated[str, Field(min_length=1)]


class ApplicationInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)
    timestamp: Text
    service: Literal["application"]
    severity: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    message: Text | None = None
    event: Text | None = None
    exception_type: Text | None = None
    request_id: Text | None = None
    method: Text | None = None
    route: Text | None = None
    status: Annotated[int, Field(ge=100, le=599)] | None = None
    duration_ms: Annotated[float, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def require_message_or_event(self) -> "ApplicationInput":
        if self.message is None and self.event is None:
            raise ValueError("application record requires message or event")
        return self


class PostgreSQLInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    timestamp: Text
    error_severity: Literal["DEBUG1", "DEBUG2", "DEBUG3", "DEBUG4", "DEBUG5",
                            "INFO", "NOTICE", "WARNING", "ERROR", "LOG", "FATAL", "PANIC"]
    message: Text
    state_code: Annotated[str, Field(pattern=r"^[0-9A-Z]{5}$")] | None = None
    detail: str | None = None
    hint: str | None = None
    context: str | None = None


STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")


class TimestampError(ValueError):
    def __init__(self, reason: str, *, timezone_error: bool = False):
        self.kind: Literal["timestamp", "timezone"] = "timezone" if timezone_error else "timestamp"
        super().__init__(reason)


def parse_timestamp(value: str, source: SourceName) -> datetime:
    candidate = value.removesuffix(" UTC") + "+00:00" if source == "postgresql.jsonl" and value.endswith(" UTC") else value
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimestampError("Invalid timestamp; use an explicit numeric offset/Z (native PostgreSQL UTC is supported)") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimestampError("Timestamp has no timezone; no local timezone is assumed", timezone_error=True)
    if not STAMP.fullmatch(candidate):
        raise TimestampError("Unsupported timestamp format or precision; require full seconds and at most six fractional digits")
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise TimestampError("Timestamp cannot be represented in UTC") from exc


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("Duplicate JSON object key")
        record[key] = value
    return record


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON numbers are unsupported")


def _float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number exceeds finite float precision/range")
    return parsed


def _unicode_scalars(raw: Any) -> None:
    pending = [raw]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            value.encode("utf-8")  # Reject escaped unpaired surrogates as well as invalid file bytes.
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def parse_record(raw: dict[str, Any], text: str, source: SourceName, line: int) -> LogRecord:
    common: dict[str, Any] = dict(source=source, line_number=line, line_id=f"{source}:{line}",
                                 raw_record=raw, raw_text=text)
    if source == "application.jsonl":
        app = ApplicationInput.model_validate(raw)
        message_field = "message" if app.message is not None else "event"
        common.update(timestamp=parse_timestamp(app.timestamp, source), service=app.service,
                      severity=app.severity, message=app.message if app.message is not None else app.event,
                      message_field=message_field, event=app.event, exception_type=app.exception_type,
                      exception_message=app.message if app.exception_type is not None else None,
                      request_id=app.request_id, method=app.method, route=app.route,
                      status=app.status, duration_ms=app.duration_ms)
    else:
        pg = PostgreSQLInput.model_validate(raw)
        common.update(timestamp=parse_timestamp(pg.timestamp, source), service="postgresql",
                      severity=pg.error_severity, message=pg.message, message_field="message",
                      sqlstate=pg.state_code, detail=pg.detail, hint=pg.hint, context=pg.context)
    return LogRecord.model_validate(common)


def _read_source(fd: int, source: SourceName) -> tuple[list[LogRecord], list[IngestionIssue], SourceReport]:
    records: list[LogRecord] = []
    errors: list[IngestionIssue] = []
    digest = hashlib.sha256()
    lines = size = 0
    state: Literal["read", "unreadable"] = "read"
    try:
        with os.fdopen(fd, "rb") as stream:
            for lines, binary in enumerate(stream, 1):
                size += len(binary)
                digest.update(binary)
                try:
                    text = binary.decode("utf-8")
                    raw = json.loads(text, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
                    if not isinstance(raw, dict):
                        raise ValueError("JSON record must be an object")
                    _unicode_scalars(raw)
                except (UnicodeDecodeError, UnicodeEncodeError):
                    errors.append(IngestionIssue(source=source, line_number=lines, kind="encoding", reason="Invalid UTF-8 or unpaired escaped Unicode surrogate"))
                    continue
                except (ValueError, RecursionError) as exc:
                    errors.append(IngestionIssue(source=source, line_number=lines, kind="json", reason=str(exc)))
                    continue
                try:
                    records.append(parse_record(raw, text, source, lines))
                except TimestampError as exc:
                    errors.append(IngestionIssue(source=source, line_number=lines, kind=exc.kind, reason=str(exc)))
                except ValidationError as exc:
                    reason = "; ".join(f"{'.'.join(map(str,e['loc'])) or 'record'}: {e['msg']}" for e in exc.errors(include_input=False))
                    errors.append(IngestionIssue(source=source, line_number=lines, kind="schema", reason=reason))
    except OSError as exc:
        state = "unreadable"
        errors.append(IngestionIssue(source=source, line_number=lines+1, kind="io", reason=str(exc)))
    return records, errors, SourceReport(source=source, state=state, physical_lines=lines,
                                         valid_records=len(records), bytes_read=size, sha256=digest.hexdigest())


def load_diagnostic_directory(path: str | Path, *, permissive: bool = False) -> IngestionResult:
    """Read only two literal filenames; strict errors retain details in IngestionError.result."""
    records: list[LogRecord] = []
    errors: list[IngestionIssue] = []
    sources: list[SourceReport] = []
    try:
        with directory_fd(Path(path)) as directory:
            for source in SOURCE_NAMES:
                try:
                    fd = open_source(directory, source)
                except OSError as exc:
                    state = "missing" if exc.errno == errno.ENOENT else "rejected"
                    sources.append(SourceReport(source=source, state=state))
                    errors.append(IngestionIssue(source=source, line_number=None,
                        kind="io" if state == "missing" else "boundary",
                        reason="Required input file is missing" if state == "missing" else f"Cannot open regular input without following symlinks: {exc}"))
                    continue
                parsed, issues, report = _read_source(fd, source)
                records.extend(parsed)
                errors.extend(issues)
                sources.append(report)
                if source == "application.jsonl" and report.state == "read" and report.physical_lines == 0:
                    errors.append(IngestionIssue(source=source, line_number=None, kind="schema",
                                                 reason="Application input is empty; at least one record is required"))
    except OSError as exc:
        errors.append(IngestionIssue(source="<diagnostic-directory>", line_number=None,
                                     kind="boundary", reason=f"Cannot open directory without following symlinks: {exc}"))
    records.sort(key=lambda record: (record.timestamp, SOURCE_NAMES.index(record.source), record.line_number))
    report = IngestionReport(mode="permissive" if permissive else "strict", complete=not errors,
                             record_count=len(records), sources=tuple(sources), errors=tuple(errors))
    result = IngestionResult(records=tuple(records), report=report)
    if errors and not permissive:
        raise IngestionError(result)
    return result
