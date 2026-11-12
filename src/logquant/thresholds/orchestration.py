"""Evaluation-only eligibility checks; never used by frozen alert inference."""
from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path
from typing import Literal,TYPE_CHECKING

from pydantic import Field
from logquant.detection.contracts import ScoringResult
from logquant.detection.core import DetectorBundle, score_run, sha256
from logquant.features import prepare_directory
from logquant.ingestion.models import Contract
from logquant.training.orchestration import CollectionMetadata, VerificationMetadata, metadata
from logquant.training.collections import require_completed_collection
from .contracts import CalibrationArtifact, CalibrationConfig, CalibrationInput, DetectorIdentity, FailureInterval
from .selection import DevelopmentCase, _select
from .storage import read_bytes
if TYPE_CHECKING:
    from .whole_run import WholeRunConfig,WholeRunArtifact

CATEGORIES = {"Database unavailable", "Invalid database credentials", "Application exception",
              "Database permission denied", "Database lock contention"}


class DevelopmentEntry(Contract):
    run_id: str = Field(min_length=1)
    split: Literal["development"]
    directory: str = Field(min_length=1)
    score_directory: str = Field(min_length=1)


class DevelopmentSelection(Contract):
    schema_version: Literal[1]
    purpose: Literal["research", "synthetic-test"]
    runs: list[DevelopmentEntry] = Field(min_length=1)


def calibrate_manifest(path: Path, bundle: DetectorBundle, detector: DetectorIdentity,
                       config: CalibrationConfig | WholeRunConfig) -> CalibrationArtifact | WholeRunArtifact:
    from .whole_run import WholeRunConfig,WholeRunCase,select as select_whole
    whole = isinstance(config,WholeRunConfig)
    if detector.model_id != bundle.metadata.model_id or detector.usage != bundle.metadata.usage:
        raise ValueError("Detector identity mismatch")
    raw, selection_hash = metadata(path)
    selection = DevelopmentSelection.model_validate(raw)
    if (selection.purpose == "synthetic-test") != (detector.usage == "synthetic-test-only"):
        raise ValueError("Synthetic/research purpose mismatch")
    ids, directories, fingerprints = set(), set(), set()
    training_ids = {p.run_id for p in bundle.metadata.training_provenance}
    training_hashes = {tuple(sorted(p.source_sha256.items())) for p in bundle.metadata.training_provenance}
    cases, inputs = [], []
    for entry in selection.runs:
        directory = Path(entry.directory)
        directory = directory if directory.is_absolute() else path.parent/directory
        score_directory = Path(entry.score_directory)
        score_directory = score_directory if score_directory.is_absolute() else path.parent/score_directory
        resolved = directory.resolve()
        if entry.run_id in ids or resolved in directories or entry.run_id in training_ids:
            raise ValueError("Duplicate or training run identity/directory")
        ids.add(entry.run_id); directories.add(resolved)
        if directory.name != entry.run_id:
            raise ValueError("Run directory basename differs from declared run ID")
        assignment_raw, assignment_hash = metadata(directory.parent/"manifest.json")
        require_completed_collection(assignment_raw, directory.parent, selection.purpose, metadata)
        collection = CollectionMetadata.model_validate(assignment_raw)
        accepted = [a for a in collection.attempts if a.status == "valid"]
        if (any(not a.run_id or not a.slot_id for a in accepted)
                or len({a.run_id for a in accepted}) != len(accepted)
                or len({a.slot_id for a in accepted}) != len(accepted)):
            raise ValueError("Missing/duplicate accepted whole-run identities or slots")
        matches = [a for a in collection.attempts if a.run_id == entry.run_id]
        if len(matches) != 1:
            raise ValueError("Missing/conflicting whole-run split assignments")
        assigned = matches[0]
        if assigned.split != "development" or assigned.status != "valid" or assigned.invalidity_reasons != []:
            raise ValueError("Selected run is not a valid development assignment")
        ground_raw, ground_hash = metadata(directory/"evaluation/ground_truth.json")
        ground = VerificationMetadata.model_validate(ground_raw)
        if whole and not ground.simulation and (ground_raw.get('collection_valid') is not True
                or ground_raw.get('collection_policy')!='acceptance-v4-whole-run'):
            raise ValueError('Whole-run policy requires amended complete research verification')
        if not whole and ground_raw.get('collection_policy')=='acceptance-v4-whole-run':
            raise ValueError('Amended whole-run verification cannot select a legacy timing threshold')
        if ground.run_id != entry.run_id or not ground.valid or ground.invalidity_reasons:
            raise ValueError("Invalid or mismatched run verification")
        if ground.simulation != (selection.purpose == "synthetic-test"):
            raise ValueError("Synthetic verification mismatch")
        recovered = datetime.fromisoformat(ground.recovery_verified_at.replace("Z", "+00:00"))
        if recovered.tzinfo is None or recovered.utcoffset() is None:
            raise ValueError("Recovery verification requires timezone")
        category = ground.fault_category
        if ("category" not in ground.config or "category" not in assigned.config
                or ground.config["category"] != category or assigned.config["category"] != category):
            raise ValueError("Fault designation/configuration mismatch")
        failure = None
        interval = ground_raw.get("observed_failure_interval")
        if category is None:
            # Fixture schema v2 explicitly writes [null, null] for healthy runs.
            # This is an absent interval, not permission to ignore partial/conflicting times.
            if ground.designation != "healthy" or interval not in (None, [None, None]):
                raise ValueError("Healthy verification has conflicting designation/failure interval")
        else:
            if category not in CATEGORIES or ground.designation != "faulty":
                raise ValueError('Unsupported faulty run designation')
            if whole:
                if not ground.simulation and (ground_raw.get('collection_valid') is not True
                        or ground_raw.get('collection_policy')!='acceptance-v4-whole-run'):
                    raise ValueError('Whole-run calibration requires amended verified collection')
            elif not isinstance(interval, list) or len(interval) != 2:
                raise ValueError("Fault verification requires a supported category and sampled failure interval")
            if not whole:
                failure = FailureInterval.model_validate_json(json.dumps(dict(start=interval[0], end=interval[1])))
                if failure.end > recovered:
                    raise ValueError("Observed failure interval ends after verified recovery")
        run = prepare_directory(directory/"diagnostic", run_id=entry.run_id)
        hashes = {s.source: s.sha256 for s in run.ingestion_report.sources}
        fingerprint = tuple(sorted(hashes.items()))
        if fingerprint in fingerprints or fingerprint in training_hashes:
            raise ValueError("Duplicate development content or overlap with detector training content")
        fingerprints.add(fingerprint)
        counts = {"application": sum(f.source == "application.jsonl" for f in run.features),
                  "postgresql": sum(f.source == "postgresql.jsonl" for f in run.features)}
        if ground.log_counts != counts:
            raise ValueError("Verified diagnostic counts mismatch")
        # Read saved detector scores and report, then verify against frozen inference.
        report, report_hash = metadata(score_directory/"score-report.json")
        data = read_bytes(score_directory/"scores.jsonl")
        rows = [json.loads(line) for line in data.decode().splitlines()]
        saved = ScoringResult.model_validate_json(json.dumps({**{k: report[k] for k in
            ("model_id", "model_usage", "score_definition", "alert_threshold", "input_sha256")}, "scores": rows}))
        expected = score_run(bundle, run)
        if saved != expected:
            raise ValueError("Saved scores differ from complete fixed-detector inference")
        expected_counts = dict(window_count=len(saved.scores), scored=sum(w.status == "scored" for w in saved.scores),
            empty_documents=sum(w.status == "empty_document" for w in saved.scores),
            zero_vectors=sum(w.status == "zero_vector" for w in saved.scores),
            coverage_unknown=sum(w.coverage_unknown for w in saved.scores))
        if any(report.get(k) != v for k, v in expected_counts.items()):
            raise ValueError("Incomplete/mismatched score report")
        inputs.append(CalibrationInput(run_id=entry.run_id, directory=str(resolved),
            score_directory=str(score_directory.resolve()), hashes={**hashes,
            "verification": ground_hash, "assignments": assignment_hash, "score_report": report_hash,
            "scores": sha256(data)}))
        cases.append(WholeRunCase(saved,category is not None,ground_raw.get('timing_status','unassessable'))
                     if whole else DevelopmentCase(scores=saved,failure=failure))
    return select_whole(cases,detector,config,inputs,selection_hash) if whole else _select(cases, detector, config, inputs, selection_hash)
