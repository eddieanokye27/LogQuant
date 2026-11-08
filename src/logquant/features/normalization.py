"""Explicit contextual feature rules. Nothing modifies a LogRecord or its raw evidence."""
import ipaddress
import json
import re

from logquant.ingestion import LogRecord
from .models import FeatureRecord

UUID_BODY = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
UUID = re.compile(r"(?<![\w])" + UUID_BODY + r"(?![\w])")
REQUEST_ID = re.compile(
    r"\b(?P<label>request[_ -]?id\s*[:=]\s*)(?P<quote>[\"']?)"
    r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?P=quote)", re.IGNORECASE)
ROUTE_ID = re.compile(
    r"(?<![\w/])(/products/)(?:\d+|" + UUID_BODY + r")"
    r"(?=/quote(?:[?\#\s\"']|$)|[?\#\s\"']|$)")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]*:){2,}[0-9A-Fa-f:.]*(?:%[A-Za-z0-9_.-]+)?(?![\w:])")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
PROCESS = re.compile(r"\b(process|PID) (\d+)\b", re.IGNORECASE)
HOLDER = re.compile(r"(\bProcess holding the lock:\s*)\d+", re.IGNORECASE)
WAIT_QUEUE = re.compile(r"(\bWait queue:\s*)(\d+(?:,\s*\d+)*)", re.IGNORECASE)
OBJECT_ID = re.compile(r"\b(relation|database) (\d+)\b")


def _address(match: re.Match[str], family: int) -> str:
    original = match.group()
    value = original.rstrip(".")  # Keep sentence punctuation outside the address.
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return original
    if parsed.version != family:
        return original
    return ("<IPV4>" if family == 4 else "<IPV6>") + original[len(value):]


def normalize_text(text: str, *, postgresql: bool = False) -> str:
    result = REQUEST_ID.sub(lambda m: m["label"] + m["quote"] + "<REQUEST_ID>" + m["quote"], text)
    result = ROUTE_ID.sub(r"\1<ROUTE_ID>", result)
    result = UUID.sub("<UUID>", result)
    result = IPV6.sub(lambda m: _address(m, 6), result)
    result = IPV4.sub(lambda m: _address(m, 4), result)
    if postgresql:
        result = PROCESS.sub(r"\1 <PID>", result)
        result = HOLDER.sub(r"\1<PID>", result)
        result = WAIT_QUEUE.sub(lambda m: m[1]+re.sub(r"\d+", "<PID>", m[2]), result)
        result = OBJECT_ID.sub(lambda m: m[1]+(" <RELATION_ID>" if m[1]=="relation" else " <DATABASE_ID>"), result)
    return result


def normalize_record(record: LogRecord, *, run_id: str) -> FeatureRecord:
    if not run_id or not run_id.strip():
        raise ValueError("An explicit nonempty run identity is required")
    original: dict[str, str] = {"service": record.service, "severity": record.severity, "message": record.message}
    for name in ("event", "method", "route", "request_id", "exception_type", "exception_message", "sqlstate",
                 "detail", "hint", "context"):
        value = getattr(record, name)
        if value is not None:
            original[name] = value
    if record.status is not None:
        original["status"] = str(record.status)
    # A tiny explicit raw-field allowlist, not a dump of variable PG metadata.
    if record.source == "postgresql.jsonl":
        for name in ("remote_host", "application_name"):
            value = record.raw_record.get(name)
            if isinstance(value, str):
                original[name] = value
    fields = original.copy()
    text_fields = {"message", "exception_message", "detail", "hint", "context", "route", "remote_host"}
    for name in text_fields & fields.keys():
        fields[name] = normalize_text(fields[name], postgresql=record.source=="postgresql.jsonl")
    if "request_id" in fields:
        fields["request_id"] = "<REQUEST_ID>"
    # Sorted-key JSON preserves field boundaries without inventing a diagnostic sentence.
    document = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return FeatureRecord(run_id=run_id, line_id=record.line_id, source=record.source,
                         line_number=record.line_number, timestamp=record.timestamp, fields=fields,
                         document=document, changed_fields=tuple(sorted(k for k in fields if fields[k]!=original[k])))
