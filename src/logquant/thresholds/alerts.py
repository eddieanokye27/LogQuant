"""Ground-truth-free classification and grouping, shared by calibration/inference."""
from datetime import timedelta, timezone
import math

from logquant.detection.contracts import ScoringResult, WindowScore
from .contracts import (AlertResult, AlertWindow, Candidate, Coverage, DetectorIdentity,
                        CalibrationArtifact, FailureInterval, SCORE)


def ordered_scores(result: ScoringResult) -> tuple[WindowScore, ...]:
    if result.score_definition != SCORE or result.alert_threshold is not None:
        raise ValueError("Unsupported raw score convention")
    for window in result.scores:
        for value in (window.start, window.end):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("Score intervals must be UTC aware")
    windows = tuple(sorted(result.scores, key=lambda w: w.start))
    if not windows:
        raise ValueError("Complete scored input must contain observed windows")
    run_ids = {w.run_id for w in windows}
    if len(run_ids) != 1 or len({w.window_id for w in windows}) != len(windows):
        raise ValueError("Mixed runs or duplicate window identities")
    lines: set[str] = set()
    for index, window in enumerate(windows):
        if window.end-window.start != timedelta(seconds=30) or window.start.timestamp() % 30:
            raise ValueError("Scores must use epoch-aligned 30-second windows")
        if index and windows[index-1].end != window.start:
            raise ValueError("Missing/overlapping score windows")
        if window.window_id != f"{window.run_id}:{window.start.astimezone(timezone.utc).isoformat()}":
            raise ValueError("Noncanonical window identity")
        if window.coverage_unknown != (index in (0, len(windows)-1)):
            raise ValueError("Observation-edge flags do not match complete grid")
        if (window.status == "scored") != (window.anomaly_score is not None):
            raise ValueError("Null/score status mismatch")
        if window.anomaly_score is not None and not math.isfinite(window.anomaly_score):
            raise ValueError("Non-finite score")
        if window.status == "empty_document" and window.line_ids:
            raise ValueError("Empty window cannot contain evidence")
        if len(set(window.line_ids)) != len(window.line_ids) or lines.intersection(window.line_ids):
            raise ValueError("Duplicated evidence identity")
        lines.update(window.line_ids)
    return windows


def classify(result: ScoringResult, detector: DetectorIdentity, threshold: float) -> AlertResult:
    if not math.isfinite(threshold):
        raise ValueError("Threshold must be finite")
    if result.model_id != detector.model_id or result.model_usage != detector.usage:
        raise ValueError("Scores belong to a different detector")
    windows = ordered_scores(result)
    states: list[AlertWindow] = []
    groups: list[list[WindowScore]] = []
    for window in windows:
        state = ("excluded_edge" if window.coverage_unknown else "unscored" if window.anomaly_score is None
                 else "alert" if window.anomaly_score > threshold else "below_threshold")
        states.append(AlertWindow(score=window, state=state))
        if state == "alert":
            if len(states) > 1 and states[-2].state == "alert" and groups[-1][-1].end == window.start:
                groups[-1].append(window)
            else:
                groups.append([window])
    candidates = tuple(Candidate(candidate_id=f"{g[0].run_id}:candidate:{g[0].start.isoformat()}",
        run_id=g[0].run_id, start=g[0].start, end=g[-1].end, first_alert_at=g[0].end,
        window_ids=tuple(w.window_id for w in g), line_ids=tuple(line for w in g for line in w.line_ids))
        for g in groups)
    interior = [w for w in windows if not w.coverage_unknown]
    scorable = sum(w.anomaly_score is not None for w in interior)
    coverage = Coverage(run_id=windows[0].run_id, interior_seconds=30*len(interior),
        scorable_seconds=30*scorable, unscored_seconds=30*(len(interior)-scorable),
        excluded_edge_windows=sum(w.coverage_unknown for w in windows),
        unscored_windows=sum(w.anomaly_score is None for w in windows))
    return AlertResult(detector=detector, threshold=threshold, windows=tuple(states),
                       candidates=candidates, coverage=coverage)


def overlaps(candidate: Candidate, failure: FailureInterval) -> bool:
    return candidate.start < failure.end and candidate.end > failure.start


def detects(candidate: Candidate, failure: FailureInterval) -> bool:
    return overlaps(candidate, failure) and failure.start <= candidate.first_alert_at < failure.end


def apply_threshold(result: ScoringResult, detector: DetectorIdentity,
                    artifact: CalibrationArtifact) -> AlertResult:
    # Validate even model_copy/update inputs and content identity at the public boundary.
    from .storage import validate_artifact
    artifact = validate_artifact(artifact)
    if artifact.status != "selected" or artifact.threshold is None:
        raise ValueError("Unsuccessful calibration cannot classify")
    if artifact.detector != detector:
        raise ValueError("Threshold artifact belongs to a different detector fingerprint")
    return classify(result, detector, artifact.threshold)
