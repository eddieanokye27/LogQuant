"""Development-only policy search. Labels exist here, never in detector text."""
from dataclasses import dataclass
import math

from logquant.detection.contracts import ScoringResult
from .alerts import classify, detects, overlaps
from .contracts import (CalibrationArtifact, CalibrationConfig, CalibrationInput,
                        DetectorIdentity, FailureInterval, ThresholdTrial)
from .storage import seal


@dataclass(frozen=True)
class DevelopmentCase:
    scores: ScoringResult
    failure: FailureInterval | None


def _select(cases: list[DevelopmentCase], detector: DetectorIdentity, config: CalibrationConfig,
            inputs: list[CalibrationInput], selection_sha256: str) -> CalibrationArtifact:
    """Internal: eligibility and saved score integrity must be checked by orchestration."""
    cases = sorted(cases, key=lambda c: c.scores.scores[0].run_id if c.scores.scores else "")
    initial = [classify(c.scores, detector, 0.0) for c in cases]
    if len({r.coverage.run_id for r in initial}) != len(initial):
        raise ValueError("Duplicate development run")
    healthy_seconds = sum(r.coverage.scorable_seconds for c, r in zip(cases, initial) if c.failure is None)
    faulty_count = sum(c.failure is not None for c in cases)
    values = sorted({w.score.anomaly_score for r in initial for w in r.windows
                     if w.state not in ("unscored", "excluded_edge")})
    reasons = []
    if not any(c.failure is None for c in cases): reasons.append("No eligible healthy development runs")
    if not faulty_count: reasons.append("No eligible faulty development runs")
    if not healthy_seconds: reasons.append("No scorable healthy interior observation duration")
    thresholds = []
    if values:
        lower = math.nextafter(values[0], -math.inf)
        if not math.isfinite(lower): raise ValueError("Minimum score has no finite lower threshold")
        thresholds = [lower, *values]
    else:
        reasons.append("No scorable interior development windows")
    trials = []
    for threshold in thresholds:
        false_candidates = 0
        detected, overlapping, missed = [], [], []
        for case in cases:
            result = classify(case.scores, detector, threshold)
            run_id = result.coverage.run_id
            if case.failure is None:
                false_candidates += len(result.candidates)
            else:
                if any(overlaps(c, case.failure) for c in result.candidates): overlapping.append(run_id)
                if any(detects(c, case.failure) for c in result.candidates): detected.append(run_id)
                else: missed.append(run_id)
        rate = false_candidates*3600/healthy_seconds if healthy_seconds else None
        recall = len(detected)/faulty_count if faulty_count else 0.0
        trials.append(ThresholdTrial(threshold=threshold, detected_faulty_runs=tuple(detected),
            overlapping_faulty_runs=tuple(overlapping), missed_faulty_runs=tuple(missed),
            faulty_run_count=faulty_count, recall=recall, healthy_candidates=false_candidates,
            healthy_scorable_seconds=healthy_seconds, false_candidates_per_healthy_hour=rate,
            feasible=not reasons and rate <= config.max_false_candidates_per_healthy_hour
                     and recall >= config.minimum_faulty_run_recall))
    feasible = [t for t in trials if t.feasible]
    winner = min(feasible, key=lambda t: (-t.recall, t.false_candidates_per_healthy_hour, -t.threshold)) if feasible else None
    if winner is None and not reasons: reasons.append("No candidate threshold meets the explicit recall and false-alert constraints")
    return seal(dict(status="selected" if winner else "unsuccessful", reasons=reasons, detector=detector.model_dump(mode="json"),
        config=config.model_dump(mode="json"), selection_sha256=selection_sha256,
        inputs=[i.model_dump(mode="json") for i in sorted(inputs, key=lambda i: i.run_id)],
        coverage=[r.coverage.model_dump(mode="json") for r in initial],
        trials=[t.model_dump(mode="json") for t in trials], threshold=winner.threshold if winner else None))
