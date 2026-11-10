from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import runpy
from unittest.mock import patch

import pytest

from logquant.detection import DetectorConfig, score_run, save_artifact
from logquant.detection.cli import main as score_cli
from logquant.detection.contracts import ScoringResult, WindowScore
from logquant.features import prepare_directory
from logquant.thresholds.alerts import classify, apply_threshold, detects, overlaps
from logquant.thresholds.contracts import CalibrationConfig, DetectorIdentity, FailureInterval, SCORE
from logquant.thresholds.orchestration import calibrate_manifest
from logquant.thresholds.selection import DevelopmentCase, _select
from logquant.thresholds.storage import load_detector, load_threshold, save_threshold, seal
from logquant.thresholds.cli import main
from logquant.training import fit_manifest

ROOT = Path(__file__).resolve().parents[1]
TRAIN = runpy.run_path(str(ROOT/"scripts/make_synthetic_detector_example.py"))["create_example"]
DEV = runpy.run_path(str(ROOT/"scripts/make_synthetic_threshold_example.py"))["create_example"]
IDENTITY = DetectorIdentity(model_id="synthetic-fixed", usage="synthetic-test-only", payload_sha256="test-only")
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = CalibrationConfig(max_false_candidates_per_healthy_hour=0.0, minimum_faulty_run_recall=0.5)


def scores(name, values):
    rows = []
    for i, value in enumerate(values):
        start = BASE+timedelta(seconds=i*30)
        rows.append(WindowScore(run_id=name, window_id=f"{name}:{start.isoformat()}", start=start,
            end=start+timedelta(seconds=30), line_ids=(f"application.jsonl:{i+1}",) if value is not None else (),
            status="scored" if value is not None else "empty_document", anomaly_score=value,
            coverage_unknown=i in (0, len(values)-1)))
    return ScoringResult(model_id=IDENTITY.model_id, model_usage=IDENTITY.usage, score_definition=SCORE,
                         input_sha256={}, scores=tuple(rows))


def interval(start=60, end=150):
    return FailureInterval(start=BASE+timedelta(seconds=start), end=BASE+timedelta(seconds=end))


def choose(cases, config=CONFIG):
    return _select(cases, IDENTITY, config, [], "synthetic-selection")


def test_strict_boundary_grouping_and_coverage():
    result = classify(scores("one", [9.0, 0.5, 0.6, 0.6, None, 0.7, 9.0]), IDENTITY, 0.5)
    assert [w.state for w in result.windows] == ["excluded_edge", "below_threshold", "alert", "alert", "unscored", "alert", "excluded_edge"]
    assert [len(c.window_ids) for c in result.candidates] == [2, 1]
    assert result.candidates[0].first_alert_at == BASE+timedelta(seconds=90)
    assert result.candidates[0].line_ids == ("application.jsonl:3", "application.jsonl:4")
    assert result.coverage.interior_seconds == 150
    assert result.coverage.scorable_seconds == 120 and result.coverage.unscored_seconds == 30
    other = classify(scores("two", [9.0, 0.7, 9.0]), IDENTITY, 0.5)
    assert other.candidates[0].run_id == "two"
    assert other.candidates[0].candidate_id != result.candidates[0].candidate_id
    original = scores("one", [9.0, 0.5, 0.6, 0.6, None, 0.7, 9.0])
    reversed_input = original.model_copy(update={"scores": tuple(reversed(original.scores))})
    assert classify(reversed_input, IDENTITY, 0.5) == result


def test_group_count_can_increase_as_threshold_increases():
    cases = [DevelopmentCase(scores("healthy", [0.0, 0.8, 0.2, 0.8, 0.0]), None),
             DevelopmentCase(scores("faulty", [0.0, 0.9, 0.9, 0.1, 0.0]), interval())]
    artifact = choose(cases)
    assert next(t for t in artifact.trials if t.threshold == 0.1).healthy_candidates == 1
    assert next(t for t in artifact.trials if t.threshold == 0.2).healthy_candidates == 2
    assert any(t.threshold == 0.8 for t in artifact.trials)


def test_overlap_is_distinct_from_first_alert_and_half_open_contact():
    candidate = classify(scores("one", [0.0, 0.9, 0.9, 0.9, 0.9, 0.0]), IDENTITY, 0.5).candidates[0]
    assert overlaps(candidate, interval(90, 120)) and not detects(candidate, interval(90, 120))
    assert detects(candidate, interval(60, 120))  # alert exactly at interval start
    assert not detects(candidate, interval(30, 60))  # alert exactly at end
    assert not overlaps(candidate, interval(150, 180))  # touching ends
    assert not overlaps(candidate, interval(0, 30))


def test_zero_vector_and_single_observation_edge_never_imply_health():
    result = scores("zero", [0.0, None, 0.0])
    rows = list(result.scores)
    rows[1] = rows[1].model_copy(update={"status": "zero_vector", "line_ids": ("application.jsonl:2",)})
    out = classify(result.model_copy(update={"scores": tuple(rows)}), IDENTITY, -1.0)
    assert out.windows[1].state == "unscored" and not out.candidates
    assert out.coverage.scorable_seconds == 0 and out.coverage.unscored_seconds == 30
    edge = classify(scores("single", [0.9]), IDENTITY, -1.0)
    assert edge.windows[0].state == "excluded_edge" and not edge.candidates
    assert edge.coverage.interior_seconds == 0 and edge.coverage.excluded_edge_windows == 1


def test_selection_ties_denominators_null_misses_and_roundtrip(tmp_path):
    healthy = DevelopmentCase(scores("healthy", [8.0, 0.2, None, 0.3, 8.0]), None)
    faulty = DevelopmentCase(scores("faulty", [8.0, 0.8, 0.9, 0.0, 8.0]), interval())
    missed = DevelopmentCase(scores("missed", [8.0, None, None, None, 8.0]), interval())
    artifact = choose([healthy, faulty, missed])
    assert artifact.status == "selected" and artifact.threshold == 0.8
    winner = next(t for t in artifact.trials if t.threshold == artifact.threshold)
    assert winner.recall == 0.5 and winner.faulty_run_count == 2
    assert winner.missed_faulty_runs == ("missed",)
    assert winner.healthy_scorable_seconds == 60 and winner.false_candidates_per_healthy_hour == 0
    assert artifact == choose([missed, faulty, healthy])
    save_threshold(artifact, tmp_path/"frozen")
    assert load_threshold(tmp_path/"frozen") == artifact
    with pytest.raises(FileExistsError): save_threshold(artifact, tmp_path/"frozen")
    output = apply_threshold(faulty.scores, IDENTITY, load_threshold(tmp_path/"frozen"))
    assert len(output.candidates) == 1


def test_candidate_rate_counts_grouped_alerts_not_windows():
    cases = [DevelopmentCase(scores("healthy", [0.0, 0.8, 0.8, None, 0.8, 0.0]), None),
             DevelopmentCase(scores("faulty", [0.0, 0.9, 0.9, 0.9, 0.0, 0.0]), interval())]
    artifact = choose(cases)
    trial = next(t for t in artifact.trials if t.threshold < 0)
    assert trial.healthy_candidates == 2 and trial.healthy_scorable_seconds == 90
    assert trial.false_candidates_per_healthy_hour == 80


def test_overlap_only_fault_is_retained_as_missed():
    artifact = choose([DevelopmentCase(scores("healthy", [0.0, 0.1, 0.1, 0.1, 0.0]), None),
        DevelopmentCase(scores("faulty", [0.0, 0.9, 0.9, 0.9, 0.0]), interval(90, 110))])
    assert artifact.status == "unsuccessful"
    trial = next(t for t in artifact.trials if t.threshold == 0.1)
    assert trial.overlapping_faulty_runs == ("faulty",) and trial.missed_faulty_runs == ("faulty",)


@pytest.mark.parametrize("kind", ["infeasible", "no_healthy_duration", "no_faults", "no_healthy", "all_null"])
def test_unsuccessful_outcomes_retained_and_not_applicable(tmp_path, kind):
    healthy = DevelopmentCase(scores("healthy", [0.0, 0.8, 0.8, 0.8, 0.0]), None)
    faulty = DevelopmentCase(scores("faulty", [0.0, 0.8, 0.8, 0.8, 0.0]), interval())
    cases = [healthy, faulty]
    if kind == "no_healthy_duration": cases[0] = DevelopmentCase(scores("healthy", [0.0, None, None, None, 0.0]), None)
    if kind == "no_faults": cases = [healthy]
    if kind == "no_healthy": cases = [faulty]
    if kind == "all_null": cases = [DevelopmentCase(scores("healthy", [None]*5), None), DevelopmentCase(scores("faulty", [None]*5), interval())]
    artifact = choose(cases)
    assert artifact.status == "unsuccessful" and artifact.threshold is None and artifact.reasons
    save_threshold(artifact, tmp_path/"unsuccessful")
    assert load_threshold(tmp_path/"unsuccessful") == artifact
    with pytest.raises(ValueError, match="Unsuccessful"): apply_threshold(faulty.scores, IDENTITY, artifact)


@pytest.mark.parametrize("config", [{}, {"max_false_candidates_per_healthy_hour": -1.0, "minimum_faulty_run_recall": 0.5},
    {"max_false_candidates_per_healthy_hour": math.inf, "minimum_faulty_run_recall": 0.5},
    {"max_false_candidates_per_healthy_hour": 0.0, "minimum_faulty_run_recall": 0.0},
    {"max_false_candidates_per_healthy_hour": 0.0, "minimum_faulty_run_recall": 1.1}])
def test_explicit_config_validation(config):
    with pytest.raises(ValueError): CalibrationConfig(**config)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "edge", "mixed", "nan", "null_status", "timezone", "convention", "identity", "duplicate_line"])
def test_invalid_score_grids_rejected(kind):
    result = scores("run", [0.0, 0.4, 0.6, 0.0])
    rows = list(result.scores)
    if kind == "missing": rows.pop(1)
    elif kind == "duplicate": rows[1] = rows[0]
    elif kind == "edge": rows[1] = rows[1].model_copy(update={"coverage_unknown": True})
    elif kind == "mixed": rows[1] = rows[1].model_copy(update={"run_id": "other"})
    elif kind == "nan": rows[1] = rows[1].model_copy(update={"anomaly_score": math.nan})
    elif kind == "null_status": rows[1] = rows[1].model_copy(update={"anomaly_score": None})
    elif kind == "timezone": rows[1] = rows[1].model_copy(update={"start": rows[1].start.replace(tzinfo=None)})
    elif kind == "identity": rows[1] = rows[1].model_copy(update={"window_id": "wrong"})
    elif kind == "duplicate_line": rows[1] = rows[1].model_copy(update={"line_ids": rows[0].line_ids})
    if kind == "convention": result = result.model_copy(update={"score_definition": "wrong"})
    result = result.model_copy(update={"scores": tuple(rows)})
    with pytest.raises(ValueError): classify(result, IDENTITY, 0.5)


@pytest.fixture
def development(tmp_path):
    train = TRAIN(tmp_path/"train")
    bundle = fit_manifest(train, config=DetectorConfig(n_estimators=12))
    save_artifact(bundle, tmp_path/"test-only-model")
    bundle, identity = load_detector(tmp_path/"test-only-model")
    selection = DEV(tmp_path/"dev")
    for entry in json.loads(selection.read_text())["runs"]:
        assert score_cli(["score", str(selection.parent/entry["directory"]/"diagnostic"), "--run-id", entry["run_id"],
            "--artifact-dir", str(tmp_path/"test-only-model"), "--output-dir", str(selection.parent/entry["score_directory"])]) == 0
    return selection, bundle, identity


def read(path): return json.loads(path.read_text())
def write(path, value): path.write_text(json.dumps(value, indent=2)+"\n")


@pytest.mark.parametrize("kind", ["train", "test", "selection_test", "validation", "invalid", "incomplete", "duplicate", "slot", "conflict", "counts", "fault_config", "missing_file", "malformed", "missing_scores", "tampered_scores", "wrong_score_model", "incomplete_score_report", "symlink", "model", "purpose", "recovery", "failure_interval", "duplicate_content", "training_identity", "training_content"])
def test_development_eligibility_rejections(development, kind):
    selection, bundle, identity = development
    value = read(selection); entry = value["runs"][0]
    directory = selection.parent/entry["directory"]
    assignment_path = directory.parent/"manifest.json"; assignment = read(assignment_path)
    ground_path = directory/"evaluation/ground_truth.json"; ground = read(ground_path)
    if kind in ("train", "test"): assignment["attempts"][0]["split"] = kind
    elif kind == "selection_test": value["runs"][0]["split"] = "test"
    elif kind == "validation": assignment["purpose"] = "validation"
    elif kind == "invalid": ground["valid"] = False
    elif kind == "incomplete": assignment["status"] = "running"
    elif kind == "duplicate": value["runs"].append(deepcopy(entry))
    elif kind == "slot": assignment["attempts"][1]["slot_id"] = assignment["attempts"][0]["slot_id"]
    elif kind == "conflict": assignment["attempts"].append({**assignment["attempts"][0], "split": "test"})
    elif kind == "counts": ground["log_counts"]["application"] = 500
    elif kind == "fault_config": ground["config"]["category"] = "Application exception"
    elif kind == "missing_file": (directory/"diagnostic/postgresql.jsonl").unlink()
    elif kind == "malformed": (directory/"diagnostic/application.jsonl").write_text("{bad}\n")
    elif kind == "missing_scores": (selection.parent/entry["score_directory"]/"scores.jsonl").unlink()
    elif kind == "tampered_scores":
        p = selection.parent/entry["score_directory"]/"scores.jsonl"; rows = [json.loads(s) for s in p.read_text().splitlines()]
        rows.pop(2); p.write_text("".join(json.dumps(s)+"\n" for s in rows))
    elif kind in ("wrong_score_model", "incomplete_score_report"):
        p = selection.parent/entry["score_directory"]/"score-report.json"; report = read(p)
        report["model_id" if kind == "wrong_score_model" else "window_count"] = "different" if kind == "wrong_score_model" else 0
        write(p, report)
    elif kind == "symlink":
        p = directory/"diagnostic/application.jsonl"
        other = directory/"original-log"; p.rename(other); p.symlink_to(other)
    elif kind == "model": identity = identity.model_copy(update={"model_id": "other"})
    elif kind == "purpose": value["purpose"] = "research"
    elif kind == "recovery": ground["recovery_verified_at"] = "2026-01-01"
    elif kind == "failure_interval": ground["observed_failure_interval"] = ["bad", "bad"]
    elif kind == "duplicate_content":
        other = selection.parent/value["runs"][1]["directory"]/"diagnostic/application.jsonl"
        other.write_bytes((directory/"diagnostic/application.jsonl").read_bytes())
    elif kind == "training_identity":
        value["runs"][0]["run_id"] = bundle.metadata.training_provenance[0].run_id
    elif kind == "training_content":
        (directory/"diagnostic/application.jsonl").write_bytes(Path(bundle.metadata.training_provenance[0].diagnostic_directory,"application.jsonl").read_bytes())
    write(selection, value); write(assignment_path, assignment); write(ground_path, ground)
    from logquant.ingestion import IngestionError
    with patch("logquant.thresholds.orchestration._select", side_effect=AssertionError("Must validate first")):
        with pytest.raises((ValueError, OSError, IngestionError)):
            calibrate_manifest(selection, bundle, identity, CONFIG)


def test_installed_workflow_frozen_inference_and_infeasible_output(development, tmp_path):
    selection, bundle, identity = development
    model = tmp_path/"test-only-model"
    threshold = tmp_path/"threshold"
    assert main(["calibrate", str(selection), "--config", str(selection.parent/"demo-policy.json"),
        "--detector-dir", str(model), "--output-dir", str(threshold)]) == 0
    artifact = load_threshold(threshold)
    source = selection.parent/"collection/synthetic-dev-faulty/diagnostic"
    with patch("logquant.thresholds.orchestration.metadata", side_effect=AssertionError("No truth reads")), \
         patch("sklearn.ensemble.IsolationForest.fit", side_effect=AssertionError("Frozen")), \
         patch("sklearn.feature_extraction.text.TfidfVectorizer.fit_transform", side_effect=AssertionError("Frozen")), \
         patch("logquant.thresholds.selection._select", side_effect=AssertionError("No retuning")):
        assert main(["apply", str(source), "--run-id", "synthetic-dev-faulty", "--threshold-dir", str(threshold),
            "--detector-dir", str(model), "--output-dir", str(tmp_path/"alerts")]) == 0
    report = read(tmp_path/"alerts/alert-report.json")
    assert report["candidates"] == 1 and report["coverage"]["scorable_seconds"] == 150
    assert report["threshold_artifact_id"] == artifact.artifact_id
    assert main(["calibrate", str(selection), "--config", str(selection.parent/"infeasible-policy.json"),
        "--detector-dir", str(model), "--output-dir", str(tmp_path/"unsuccessful")]) == 3
    assert load_threshold(tmp_path/"unsuccessful").status == "unsuccessful"
    assert main(["apply", str(source), "--run-id", "synthetic-dev-faulty", "--threshold-dir", str(tmp_path/"unsuccessful"),
        "--detector-dir", str(model), "--output-dir", str(tmp_path/"bad-alerts")]) == 1
    assert not (tmp_path/"bad-alerts").exists()


def test_calibration_metadata_never_enters_vectorization(development):
    selection, bundle, identity = development
    from sklearn.feature_extraction.text import TfidfVectorizer
    for entry in read(selection)["runs"]:
        path = selection.parent/entry["directory"]/"evaluation/ground_truth.json"
        ground = read(path); ground["note"] = "SECRET_VERIFICATION_LABEL"; write(path, ground)
    seen = []
    original = TfidfVectorizer.transform
    def transform(self, documents):
        seen.extend(documents)
        assert all("SECRET_VERIFICATION_LABEL" not in d and "Application exception" not in d for d in documents)
        return original(self, documents)
    with patch.object(TfidfVectorizer, "transform", transform), \
         patch.object(TfidfVectorizer, "fit_transform", side_effect=AssertionError("No fitting")):
        result = calibrate_manifest(selection, bundle, identity,
            CalibrationConfig(max_false_candidates_per_healthy_hour=30.0, minimum_faulty_run_recall=1.0))
    assert len(seen) == 14 and result.status == "selected"


@pytest.mark.parametrize("kind", ["detector", "policy", "convention", "checksum"])
def test_incompatible_frozen_artifacts_rejected(kind):
    artifact = choose([DevelopmentCase(scores("healthy", [0.0, 0.2, 0.2, 0.0]), None),
        DevelopmentCase(scores("faulty", [0.0, 0.8, 0.9, 0.0]), interval(60, 110))])
    assert artifact.status == "selected"
    result = scores("new-run", [0.0, 0.8, 0.9, 0.0])
    identity = IDENTITY
    if kind == "detector": identity = identity.model_copy(update={"payload_sha256": "other"})
    elif kind == "policy": artifact = artifact.model_copy(update={"policy": "wrong"})
    elif kind == "convention": artifact = artifact.model_copy(update={"score_definition": "wrong"})
    else: artifact = artifact.model_copy(update={"threshold": 0.7})
    with pytest.raises(ValueError): apply_threshold(result, identity, artifact)
