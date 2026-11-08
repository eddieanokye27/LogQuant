"""Sparse fitting internals and frozen inference. This module never reads run metadata."""
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.feature_extraction.text import TfidfVectorizer

from logquant.features import FeatureRun, FeatureWindow, build_windows
from logquant.features.windowing import ANCHOR
from .contracts import (DetectorConfig, ModelMetadata, ScoringResult, TrainingProvenance,
                        TrainingWindow, WindowScore)

ROOT = Path(__file__).resolve().parents[3]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dependency_versions() -> dict[str, str]:
    versions = {d.metadata["Name"].lower().replace("_","-"):d.version
                for d in importlib.metadata.distributions() if d.metadata["Name"].lower()!="logquant"}
    return {"python": platform.python_version(), "platform": sys.platform+"/"+platform.machine(),
            **dict(sorted(versions.items()))}


def implementation_sha256() -> str:
    source = Path(__file__).resolve().parents[1]
    paths = sorted(p for p in source.rglob("*.py") if "__pycache__" not in p.parts)
    return sha256(b"".join(str(p.relative_to(source)).encode()+b"\0"+p.read_bytes() for p in paths))


def validate_run(run: FeatureRun) -> tuple[FeatureWindow, ...]:
    if not run.ingestion_report.complete or run.ingestion_report.errors:
        raise ValueError("Incomplete ingestion is not eligible for modeling")
    if run.ingestion_report.record_count != len(run.features):
        raise ValueError("Feature count does not match complete ingestion")
    if run.alignment_anchor != ANCHOR:
        raise ValueError("Unsupported window alignment anchor")
    expected = build_windows(run.features, run_id=run.run_id)
    supplied = tuple(sorted(run.windows, key=lambda w: (w.start, w.index)))
    if supplied != expected:
        raise ValueError("Feature/window contract mismatch: missing, altered or mixed windows")
    return expected


@dataclass(frozen=True)
class DetectorBundle:
    vectorizer: TfidfVectorizer
    detector: IsolationForest
    metadata: ModelMetadata


def _fit_prepared(runs: Sequence[FeatureRun], *, provenance: Sequence[TrainingProvenance],
                  selection_sha256: str, usage: str, config: DetectorConfig) -> DetectorBundle:
    """Internal API: callers must first establish eligibility in training.orchestration."""
    if not runs:
        raise ValueError("At least one eligible healthy training run is required")
    if len({r.run_id for r in runs}) != len(runs):
        raise ValueError("Duplicate training run identity")
    ordered = sorted(runs, key=lambda r: r.run_id)
    audit: list[TrainingWindow] = []
    candidates: list[FeatureWindow] = []
    for run in ordered:
        for window in validate_run(run):
            status = "excluded_edge" if window.first_observed_bucket or window.last_observed_bucket else (
                "empty_document" if not window.document.strip() else "fit")
            audit.append(TrainingWindow(run_id=run.run_id, window_id=window.window_id,
                document_sha256=sha256(window.document.encode()), status=status))
            if status == "fit":
                candidates.append(window)
    if not candidates:
        raise ValueError("No usable training documents: all interior documents are empty or all windows are coverage-unknown edges")
    vectorizer = TfidfVectorizer(ngram_range=(config.ngram_min,config.ngram_max),
        max_features=config.max_features, lowercase=False, sublinear_tf=True, norm="l2",
        min_df=1, max_df=1.0, stop_words=None, dtype=np.float64)
    try:
        # This is the only fitted text: complete healthy training WINDOW DOCUMENT fields.
        matrix = vectorizer.fit_transform([w.document for w in candidates]).tocsr()
    except ValueError as exc:
        raise ValueError("Training documents produce no usable TF-IDF vocabulary") from exc
    nonzero = np.asarray(matrix.getnnz(axis=1)).ravel() > 0
    zero_ids = {w.window_id for w, used in zip(candidates, nonzero) if not used}
    audit = [row.model_copy(update={"status":"zero_vector"}) if row.window_id in zero_ids else row for row in audit]
    count = int(nonzero.sum())
    if count < 2:
        raise ValueError("Need at least two nonzero healthy interior training windows")
    samples = min(config.max_samples,count)
    detector = IsolationForest(n_estimators=config.n_estimators, max_samples=samples,
        random_state=config.seed, n_jobs=1, contamination="auto", bootstrap=False, max_features=1.0)
    detector.fit(matrix[nonzero].tocsc())
    payload = dict(usage=usage,config=config.model_dump(mode="json"),selection_manifest_sha256=selection_sha256,
        training_provenance=[p.model_dump(mode="json") for p in sorted(provenance,key=lambda p:p.run_id)],
        training_windows=[r.model_dump(mode="json") for r in audit], vectorizer_fit_windows=len(candidates),
        detector_fit_windows=count,effective_max_samples=samples,vocabulary_size=len(vectorizer.vocabulary_),
        fixed_parameters={"lowercase":"False","sublinear_tf":"True","norm":"l2","min_df":"1",
                          "max_df":"1.0","dtype":"float64","token_pattern":vectorizer.token_pattern,
                          "contamination":"auto (internal offset unused)","n_jobs":"1",
                          "bootstrap":"False","forest_max_features":"1.0"},
        dependency_versions=dependency_versions(),analysis_lock_sha256=sha256((ROOT/"requirements-analysis.lock").read_bytes()),
        implementation_sha256=implementation_sha256())
    payload["model_id"] = sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode())
    metadata = ModelMetadata.model_validate_json(json.dumps(payload))
    return DetectorBundle(vectorizer,detector,metadata)


def score_run(bundle: DetectorBundle, run: FeatureRun) -> ScoringResult:
    """Transform/score only. Empty and unseen-vocabulary rows are explicitly unscorable."""
    windows = validate_run(run)
    documents = [w.document for w in windows]
    statuses = ["empty_document" if not doc.strip() else "zero_vector" for doc in documents]
    scores: list[float | None] = [None]*len(windows)
    if windows:
        matrix = bundle.vectorizer.transform(documents).tocsr()
        indices = np.flatnonzero(np.asarray(matrix.getnnz(axis=1)).ravel() > 0)
        if len(indices):
            values = -bundle.detector.score_samples(matrix[indices].tocsr())
            if not np.all(np.isfinite(values)):
                raise ValueError("Detector produced non-finite anomaly scores")
            for index,value in zip(indices,values):
                statuses[int(index)] = "scored"
                scores[int(index)] = float(value)
    return ScoringResult(model_id=bundle.metadata.model_id,model_usage=bundle.metadata.usage,
        score_definition=bundle.metadata.score_definition,
        input_sha256={s.source:s.sha256 for s in run.ingestion_report.sources if s.sha256 is not None},
        scores=tuple(WindowScore(run_id=w.run_id,window_id=w.window_id,start=w.start,end=w.end,line_ids=w.line_ids,
            status=status,anomaly_score=value,coverage_unknown=w.first_observed_bucket or w.last_observed_bucket)
            for w,status,value in zip(windows,statuses,scores)))
