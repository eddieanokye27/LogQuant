from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import runpy
from unittest.mock import patch

import numpy as np
import pytest
from scipy import sparse
from sklearn.ensemble import IsolationForest
from sklearn.feature_extraction.text import TfidfVectorizer

from logquant.detection import DetectorConfig, load_trusted_artifact, save_artifact, score_run
from logquant.detection.cli import main
from logquant.features import prepare_directory
from logquant.training import fit_manifest

CREATE=runpy.run_path(str(Path(__file__).resolve().parents[1]/"scripts/make_synthetic_detector_example.py"))["create_example"]
FAST=DetectorConfig(n_estimators=12)


@pytest.fixture
def selected(tmp_path):
    return CREATE(tmp_path/"synthetic-test-only")


def read(path):return json.loads(path.read_text())
def write(path,value):path.write_text(json.dumps(value,indent=2)+"\n")
def first_run(selection):
    return selection.parent/read(selection)["runs"][0]["directory"]


@pytest.mark.parametrize("changes",[{"seed":True},{"seed":-1},{"n_estimators":0},{"max_samples":1},
                                    {"max_features":0},{"ngram_min":3,"ngram_max":1},{"contamination":.1}])
def test_invalid_config_rejected(changes):
    with pytest.raises(ValueError):DetectorConfig(**changes)


@pytest.mark.parametrize("case",["nontrain_selection","nontrain_assignment","fault_assignment","fault_verification",
                                "invalid_verification","invalid_assignment","incomplete_collection","missing_recovery",
                                "missing_input","validation_collection","duplicate_id","duplicate_directory",
                                "duplicate_content","conflicting_split","duplicate_slot","per_window_selection",
                                "simulation_mismatch","count_mismatch","cross_split_slot"])
def test_training_eligibility_rejected_before_fit(selected,case):
    selection=read(selected);directory=first_run(selected)
    assignments_path=directory.parent/"manifest.json";assignments=read(assignments_path)
    ground_path=directory/"evaluation/ground_truth.json";ground=read(ground_path)
    if case=="nontrain_selection":selection["runs"][0]["split"]="development"
    elif case=="nontrain_assignment":assignments["attempts"][0]["split"]="test"
    elif case=="fault_assignment":assignments["attempts"][0]["config"]["category"]="Database unavailable"
    elif case=="fault_verification":ground["fault_category"]="Application exception"
    elif case=="invalid_verification":ground["valid"]=False
    elif case=="invalid_assignment":assignments["attempts"][0]["status"]="invalid"
    elif case=="incomplete_collection":assignments["status"]="running"
    elif case=="missing_recovery":ground.pop("recovery_verified_at")
    elif case=="missing_input":(directory/"diagnostic/postgresql.jsonl").unlink()
    elif case=="validation_collection":assignments["purpose"]="validation"
    elif case=="duplicate_id":selection["runs"].append(deepcopy(selection["runs"][0]))
    elif case=="duplicate_directory":selection["runs"][1]["directory"]=selection["runs"][0]["directory"]
    elif case=="duplicate_content":
        second=selected.parent/selection["runs"][1]["directory"]/"diagnostic/application.jsonl"
        second.write_bytes((directory/"diagnostic/application.jsonl").read_bytes())
    elif case=="conflicting_split":assignments["attempts"].append({**assignments["attempts"][0],"split":"test"})
    elif case=="duplicate_slot":assignments["attempts"][1]["slot_id"]=assignments["attempts"][0]["slot_id"]
    elif case=="per_window_selection":selection["runs"][0]["window_ids"]=["pick-one"]
    elif case=="simulation_mismatch":ground["simulation"]=False
    elif case=="count_mismatch":ground["log_counts"]["application"]=400
    elif case=="cross_split_slot":assignments["attempts"].append({**assignments["attempts"][0],"run_id":"unselected-test","split":"test"})
    write(selected,selection);write(assignments_path,assignments);write(ground_path,ground)
    with patch("logquant.training.orchestration._fit_prepared",side_effect=AssertionError("must not fit")):
        with pytest.raises(ValueError):fit_manifest(selected,config=FAST)


def test_metadata_exclusion_exact_documents_and_sparse_fit(selected):
    selection=read(selected)
    directory=first_run(selected)
    ground_path=directory/"evaluation/ground_truth.json";ground=read(ground_path)
    ground["note"]="SECRETVERIFICATIONTOKEN";write(ground_path,ground)
    manifest_path=directory.parent/"manifest.json";manifest=read(manifest_path)
    manifest["note"]="SECRETASSIGNMENTTOKEN";write(manifest_path,manifest)
    expected=[]
    for entry in sorted(selection["runs"],key=lambda e:e["run_id"]):
        run=prepare_directory(selected.parent/entry["directory"]/"diagnostic",run_id=entry["run_id"])
        expected += [w.document for w in run.windows if not(w.first_observed_bucket or w.last_observed_bucket)]
    original_tfidf=TfidfVectorizer.fit_transform;original_forest=IsolationForest.fit
    seen=[]
    def tfidf(self,documents,*args,**kwargs):
        assert documents==expected
        return original_tfidf(self,documents,*args,**kwargs)
    def forest(self,matrix,*args,**kwargs):
        assert sparse.isspmatrix_csc(matrix)
        seen.append(matrix.shape)
        return original_forest(self,matrix,*args,**kwargs)
    with patch.object(TfidfVectorizer,"fit_transform",tfidf),patch.object(IsolationForest,"fit",forest):
        bundle=fit_manifest(selected,config=FAST)
    assert seen[0][0]==9
    vocabulary=" ".join(bundle.vectorizer.vocabulary_)
    for forbidden in ("SECRETVERIFICATIONTOKEN","SECRETASSIGNMENTTOKEN","synthetic-train","ground_truth.json","application.jsonl"):
        assert forbidden not in vocabulary
    assert bundle.metadata.usage=="synthetic-test-only" and bundle.metadata.alert_threshold is None
    assert len(bundle.metadata.training_provenance)==3
    assert sum(w.status=="excluded_edge" for w in bundle.metadata.training_windows)==6


def test_deterministic_training_order_and_frozen_scoring(selected):
    one=fit_manifest(selected,config=FAST)
    selection=read(selected);selection["runs"].reverse();write(selected,selection)
    two=fit_manifest(selected,config=FAST)
    assert one.vectorizer.vocabulary_==two.vectorizer.vocabulary_
    assert np.array_equal(one.vectorizer.idf_,two.vectorizer.idf_)
    source=selected.parent/"synthetic-score/diagnostic"
    run=prepare_directory(source,run_id="synthetic-score")
    vocabulary=one.vectorizer.vocabulary_.copy();idf=one.vectorizer.idf_.copy()
    trees=[e.tree_.threshold.copy() for e in one.detector.estimators_]
    with patch.object(one.vectorizer,"fit",side_effect=AssertionError("no fitting")), \
         patch.object(one.vectorizer,"fit_transform",side_effect=AssertionError("no fitting")), \
         patch.object(one.detector,"fit",side_effect=AssertionError("no fitting")):
        scored=score_run(one,run)
    again=score_run(two,run)
    assert scored.scores==again.scores
    assert vocabulary==one.vectorizer.vocabulary_ and np.array_equal(idf,one.vectorizer.idf_)
    assert all(np.array_equal(a,e.tree_.threshold) for a,e in zip(trees,one.detector.estimators_))


def test_score_alignment_empty_windows_and_unknown_edges(selected):
    bundle=fit_manifest(selected,config=FAST)
    run=prepare_directory(selected.parent/"synthetic-score/diagnostic",run_id="synthetic-score")
    original=bundle.detector.score_samples
    formats=[]
    def sparse_score(matrix):
        assert sparse.isspmatrix_csr(matrix);formats.append(matrix.shape)
        return original(matrix)
    with patch.object(bundle.detector,"score_samples",side_effect=sparse_score):out=score_run(bundle,run)
    assert len(out.scores)==5 and formats[0][0]==3
    assert [s.window_id for s in out.scores]==[w.window_id for w in run.windows]
    assert [s.status for s in out.scores]==["scored","empty_document","empty_document","scored","scored"]
    assert [s.coverage_unknown for s in out.scores]==[True,False,False,False,True]
    values=-original(bundle.vectorizer.transform([w.document for w in run.windows if w.document]).tocsr())
    assert np.allclose([s.anomaly_score for s in out.scores if s.status=="scored"],values)
    assert all(s.anomaly_score is None for s in out.scores if s.status!="scored")
    assert out.alert_threshold is None


def test_all_empty_eligible_training_rejected(selected):
    for entry in read(selected)["runs"]:
        directory=selected.parent/entry["directory"]
        source=directory/"diagnostic/application.jsonl"
        rows=[json.loads(s) for s in source.read_text().splitlines()]
        source.write_text("".join(json.dumps(r)+"\n" for r in (rows[0],rows[-1])))
        ground_path=directory/"evaluation/ground_truth.json";g=read(ground_path);g["log_counts"]["application"]=2;write(ground_path,g)
    with pytest.raises(ValueError,match="empty"):fit_manifest(selected,config=FAST)


@pytest.mark.parametrize("case", ["no_events", "edge_only", "malformed"])
def test_unusable_training_inputs_fail_explicitly(selected, case):
    for entry in read(selected)["runs"]:
        directory = selected.parent / entry["directory"]
        path = directory / "diagnostic/application.jsonl"
        rows = path.read_text().splitlines()
        path.write_text("" if case == "no_events" else rows[0] + "\n")
        if case == "malformed":
            path.write_text("{not json}\n")
        ground_path = directory / "evaluation/ground_truth.json"
        ground = read(ground_path)
        ground["log_counts"]["application"] = 0 if case == "no_events" else 1
        write(ground_path, ground)
    from logquant.ingestion import IngestionError
    with pytest.raises((ValueError, IngestionError)):
        fit_manifest(selected, config=FAST)


def test_no_observations_rejected_without_health_claim(selected, tmp_path):
    bundle = fit_manifest(selected, config=FAST)
    source = selected.parent / "synthetic-score/diagnostic"
    (source / "application.jsonl").write_text("")
    destination = tmp_path / "test-only-model"
    save_artifact(bundle, destination)
    from logquant.ingestion import IngestionError
    with pytest.raises(IngestionError, match="Application input is empty"):
        prepare_directory(source, run_id="synthetic-score")
    with patch.object(bundle.detector, "score_samples", side_effect=AssertionError("No observations")):
        assert main(["score", str(source), "--run-id", "synthetic-score", "--artifact-dir",
                     str(destination), "--output-dir", str(tmp_path / "scores")]) == 2
    assert not (tmp_path / "scores").exists()


def test_unseen_vocabulary_vectors_are_unscorable_not_healthy(selected):
    for entry in read(selected)["runs"]:
        path=selected.parent/entry["directory"]/"diagnostic/application.jsonl"
        rows=[json.loads(s) for s in path.read_text().splitlines()]
        for row in rows:row["message"]=" ".join(["alpha"]*100)
        path.write_text("".join(json.dumps(r)+"\n" for r in rows))
    bundle=fit_manifest(selected,config=DetectorConfig(n_estimators=12,max_features=1,ngram_max=1))
    source=selected.parent/"synthetic-score/diagnostic"
    path=source/"application.jsonl";rows=[json.loads(s) for s in path.read_text().splitlines()]
    for row in rows:row["message"]=" ".join(["quasarunknown"]*100)
    path.write_text("".join(json.dumps(r)+"\n" for r in rows))
    run=prepare_directory(source,run_id="synthetic-score")
    with patch.object(bundle.detector,"score_samples",side_effect=AssertionError("Do not score zero vectors")):
        out=score_run(bundle,run)
    assert [s.status for s in out.scores]==["zero_vector","empty_document","empty_document","zero_vector","zero_vector"]
    assert all(s.anomaly_score is None for s in out.scores)


@pytest.mark.parametrize("nonzero_count",[1,3])
def test_training_zero_vectors_excluded_and_too_few_rows_rejected(selected,nonzero_count):
    seen=0
    for entry in read(selected)["runs"]:
        path=selected.parent/entry["directory"]/"diagnostic/application.jsonl"
        rows=[json.loads(s) for s in path.read_text().splitlines()]
        for i,row in enumerate(rows):
            row["message"]="beta"
            if i in (1,2,3):
                if seen<nonzero_count:row["message"]=" ".join(["alpha"]*100)
                seen+=1
        path.write_text("".join(json.dumps(r)+"\n" for r in rows))
    config=DetectorConfig(n_estimators=12,max_features=1,ngram_max=1)
    if nonzero_count==1:
        with pytest.raises(ValueError,match="at least two nonzero"):fit_manifest(selected,config=config)
    else:
        bundle=fit_manifest(selected,config=config)
        assert bundle.metadata.vectorizer_fit_windows==9 and bundle.metadata.detector_fit_windows==3
        assert sum(w.status=="zero_vector" for w in bundle.metadata.training_windows)==6


def test_artifact_symlinks_and_source_destinations_rejected(selected,tmp_path):
    bundle=fit_manifest(selected,config=FAST)
    with pytest.raises(ValueError,match="outside"):save_artifact(bundle,first_run(selected)/"diagnostic/model")
    destination=tmp_path/"test-only-model";save_artifact(bundle,destination)
    payload=(destination/"model.pkl").read_bytes()
    elsewhere=tmp_path/"payload";elsewhere.write_bytes(payload)
    (destination/"model.pkl").unlink();(destination/"model.pkl").symlink_to(elsewhere)
    with pytest.raises(OSError):load_trusted_artifact(destination)


def test_artifact_round_trip_and_no_overwrite(selected,tmp_path):
    bundle=fit_manifest(selected,config=FAST);destination=tmp_path/"test-only-model"
    save_artifact(bundle,destination)
    original={p.name:p.read_bytes() for p in destination.iterdir()}
    with pytest.raises(FileExistsError):save_artifact(bundle,destination)
    assert {p.name:p.read_bytes() for p in destination.iterdir()}==original
    loaded=load_trusted_artifact(destination)
    run=prepare_directory(selected.parent/"synthetic-score/diagnostic",run_id="synthetic-score")
    assert score_run(bundle,run)==score_run(loaded,run)
    assert loaded.metadata==bundle.metadata
    save_artifact(bundle,destination,overwrite=True)


@pytest.mark.parametrize("corrupt",["checksum","version","lock","implementation"])
def test_bad_artifacts_rejected_before_deserialization(selected,tmp_path,corrupt):
    bundle=fit_manifest(selected,config=FAST);destination=tmp_path/"test-only-model";save_artifact(bundle,destination)
    path=destination/"model-metadata.json";meta=read(path)
    if corrupt=="checksum":(destination/"model.pkl").write_bytes(b"NOT A MODEL")
    elif corrupt=="version":meta["metadata"]["dependency_versions"]["scikit-learn"]="0.0"
    elif corrupt=="lock":meta["metadata"]["analysis_lock_sha256"]="bad"
    else:meta["metadata"]["implementation_sha256"]="bad"
    write(path,meta)
    with patch("logquant.detection.storage.pickle.loads",side_effect=AssertionError("must not deserialize")):
        with pytest.raises(ValueError):load_trusted_artifact(destination)


def test_scoring_cli_reads_no_split_or_validity_metadata(selected,tmp_path):
    destination=tmp_path/"test-only-model"
    assert main(["fit",str(selected),"--artifact-dir",str(destination)])==0
    source=selected.parent/"synthetic-score/diagnostic";out=tmp_path/"scores"
    with patch("logquant.training.orchestration.metadata",side_effect=AssertionError("Inference must not read eligibility metadata")):
        assert main(["score",str(source),"--run-id","synthetic-score","--artifact-dir",str(destination),"--output-dir",str(out)])==0
    report=read(out/"score-report.json")
    assert report["model_usage"]=="synthetic-test-only" and report["alert_threshold"] is None
    assert report["empty_documents"]==2 and report["scored"]==3
    assert main(["score",str(source),"--run-id","synthetic-score","--artifact-dir",str(destination),"--output-dir",str(out)])==1


def test_incomplete_feature_input_is_not_scored(selected):
    bundle=fit_manifest(selected,config=FAST)
    run=prepare_directory(selected.parent/"synthetic-score/diagnostic",run_id="synthetic-score")
    broken=run.model_copy(update={"ingestion_report":run.ingestion_report.model_copy(update={"complete":False})})
    with pytest.raises(ValueError,match="Incomplete"):score_run(bundle,broken)
    with pytest.raises(ValueError,match="contract"):score_run(bundle,run.model_copy(update={"windows":run.windows[1:]}))
