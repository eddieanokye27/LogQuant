"""UTC epoch-aligned half-open windows; bounds use only observed diagnostic events."""
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from logquant.ingestion import load_diagnostic_directory
from logquant.ingestion.models import IngestionResult, SOURCE_NAMES
from .models import FeatureRecord, FeatureRun, FeatureWindow
from .normalization import normalize_record

ANCHOR = datetime(1970, 1, 1, tzinfo=timezone.utc)
WIDTH = timedelta(seconds=30)


def ordered_features(records: Sequence[FeatureRecord], run_id: str) -> tuple[FeatureRecord, ...]:
    if not run_id or not run_id.strip():
        raise ValueError("An explicit nonempty run identity is required")
    if any(r.run_id != run_id for r in records):
        raise ValueError("Cannot combine feature records from different runs")
    if len({r.line_id for r in records}) != len(records):
        raise ValueError("Duplicate evidence line IDs within a run")
    if any(r.line_id != f"{r.source}:{r.line_number}" for r in records):
        raise ValueError("Evidence line IDs must match source identity and physical line number")
    if any(r.timestamp.tzinfo is None or r.timestamp.utcoffset() is None for r in records):
        raise ValueError("Window timestamps must be timezone-aware")
    return tuple(sorted(records, key=lambda r: (r.timestamp, SOURCE_NAMES.index(r.source), r.line_number)))


def build_windows(records: Sequence[FeatureRecord], *, run_id: str) -> tuple[FeatureWindow, ...]:
    ordered = ordered_features(records, run_id)
    if not ordered:
        return ()  # No observations: no inferred intervals, including leading/trailing time.
    buckets: dict[int, list[FeatureRecord]] = defaultdict(list)
    for record in ordered:
        # Integer timedelta division avoids floating-point rounding at microsecond edges.
        bucket = (record.timestamp.astimezone(timezone.utc)-ANCHOR)//WIDTH
        buckets[bucket].append(record)
    first, last = min(buckets), max(buckets)
    windows: list[FeatureWindow] = []
    for index, bucket in enumerate(range(first, last+1)):
        start = ANCHOR+bucket*WIDTH
        items = buckets[bucket]
        try:
            end = start+WIDTH
        except OverflowError as exc:
            raise ValueError("Window end cannot be represented by a datetime") from exc
        windows.append(FeatureWindow(run_id=run_id, window_id=f"{run_id}:{start.isoformat()}", index=index,
            start=start, end=end, line_ids=tuple(r.line_id for r in items),
            document="\n".join(r.document for r in items), empty=not items,
            first_observed_bucket=bucket==first, last_observed_bucket=bucket==last))
    return tuple(windows)


def prepare_ingestion(result: IngestionResult, *, run_id: str) -> FeatureRun:
    """The supplied ingestion result must come from one explicit diagnostic directory."""
    if not result.complete:
        raise ValueError("Features require complete ingestion; partial input is not accepted")
    features = ordered_features(tuple(normalize_record(r, run_id=run_id) for r in result.records), run_id)
    windows = build_windows(features, run_id=run_id)
    return FeatureRun(run_id=run_id, alignment_anchor=ANCHOR,
        observed_first_event=features[0].timestamp if features else None,
        observed_last_event=features[-1].timestamp if features else None,
        features=features, windows=windows, ingestion_report=result.report)


def prepare_directory(path: str | Path, *, run_id: str) -> FeatureRun:
    """One directory, one strict ingestion, one run; no metadata discovery or loading."""
    return prepare_ingestion(load_diagnostic_directory(path), run_id=run_id)
