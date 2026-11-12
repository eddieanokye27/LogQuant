"""Validate whole-run assignments and independent verification before any fitting."""
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from logquant.features import prepare_directory
from logquant.ingestion.boundary import directory_fd, open_source
from logquant.ingestion.models import Contract
from logquant.detection.contracts import DetectorConfig, TrainingProvenance
from logquant.detection.core import DetectorBundle, _fit_prepared, sha256
from .collections import require_completed_collection


class SelectionEntry(Contract):
    run_id: str = Field(min_length=1)
    split: Literal["train"]
    directory: str = Field(min_length=1)


class Selection(Contract):
    schema_version: Literal[1]
    purpose: Literal["research", "synthetic-test"]
    runs: list[SelectionEntry] = Field(min_length=1)


class AssignmentMetadata(BaseModel):
    model_config=ConfigDict(strict=True,extra="allow")
    run_id: str | None = None
    slot_id: str | None = None
    split: str
    status: str
    config: dict[str, Any]
    invalidity_reasons: list[str] | None = None


class CollectionMetadata(BaseModel):
    model_config=ConfigDict(strict=True,extra="allow")
    schema_version: Literal[1]
    purpose: str
    status: str
    attempts: list[AssignmentMetadata]


class VerificationMetadata(BaseModel):
    model_config=ConfigDict(strict=True,extra="allow")
    run_id: str
    valid: bool
    invalidity_reasons: list[str]
    designation: str
    fault_category: str | None
    config: dict[str, Any]
    simulation: bool
    recovery_verified_at: str
    log_counts: dict[str, int]

    @model_validator(mode='after')
    def whole_run_validity(self):
        extra=self.model_extra or {}
        if extra.get('collection_policy')=='acceptance-v4-whole-run':
            if extra.get('collection_valid') is not self.valid or extra.get('timing_status')!='unassessable':
                raise ValueError('Conflicting amended collection validity/timing status')
            if self.valid and (not extra.get('collector_ranges') or not extra.get('representation') or not extra.get('request_links')):
                raise ValueError('Amended verification lacks complete physical-export proof')
        return self


def read_bytes(path: Path) -> bytes:
    with directory_fd(path.parent) as directory:
        with os.fdopen(open_source(directory,path.name),"rb") as stream:
            return stream.read()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key,value in items:
        if key in result: raise ValueError("Duplicate metadata JSON key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError("Non-finite metadata number")


def metadata(path: Path) -> tuple[dict[str, Any], str]:
    data = read_bytes(path)
    value = json.loads(data,object_pairs_hook=_pairs,parse_constant=_nonfinite)
    if not isinstance(value,dict): raise ValueError("Metadata must be a JSON object")
    return value,sha256(data)


def fit_manifest(path: str | Path, *, config: DetectorConfig | None = None) -> DetectorBundle:
    path = Path(path)
    raw,selection_hash = metadata(path)
    selection = Selection.model_validate(raw)
    run_ids: set[str] = set()
    directories: set[Path] = set()
    fingerprints: set[tuple] = set()
    slots: set[tuple] = set()
    prepared,provenance = [],[]
    # Entire selection is validated and prepared before _fit_prepared is invoked.
    for entry in selection.runs:
        directory = Path(entry.directory)
        directory = directory if directory.is_absolute() else path.parent/directory
        identity = directory.resolve()
        if entry.run_id in run_ids or identity in directories:
            raise ValueError("Duplicate run identity or directory in training selection")
        run_ids.add(entry.run_id);directories.add(identity)
        if directory.name != entry.run_id:
            raise ValueError("Run directory basename must match its declared identity")
        assignments,assignment_hash = metadata(directory.parent/"manifest.json")
        require_completed_collection(assignments, directory.parent, selection.purpose, metadata)
        assignments=CollectionMetadata.model_validate(assignments).model_dump()
        valid = [a for a in assignments.get("attempts",[]) if a.get("status")=="valid"]
        if any(not a["run_id"] for a in valid):raise ValueError("Accepted collection assignment has no run identity")
        if len({a["run_id"] for a in valid}) != len(valid):
            raise ValueError("Collection has duplicate accepted run assignments")
        if any(not a["slot_id"] for a in valid) or len({a["slot_id"] for a in valid}) != len(valid):
            raise ValueError("Collection has missing or duplicate accepted whole-run slots")
        matches = [a for a in assignments.get("attempts",[]) if a.get("run_id")==entry.run_id]
        if len(matches)!=1: raise ValueError("Missing or duplicate whole-run split assignment")
        assigned = matches[0]
        if assigned.get("split")!="train" or assigned.get("status")!="valid" or assigned.get("invalidity_reasons")!=[]:
            raise ValueError("Selected run is not a valid whole training run")
        if "category" not in assigned.get("config",{}) or assigned["config"]["category"] is not None:
            raise ValueError("Faulty training assignment is forbidden")
        slot = (str(directory.parent.resolve()),assigned.get("slot_id"))
        if not isinstance(slot[1],str) or not slot[1] or slot in slots:
            raise ValueError("Missing or duplicate accepted training slot")
        slots.add(slot)
        ground,ground_hash = metadata(directory/"evaluation/ground_truth.json")
        ground=VerificationMetadata.model_validate(ground).model_dump()
        if ground.get("run_id")!=entry.run_id or ground.get("valid") is not True or ground.get("invalidity_reasons")!=[]:
            raise ValueError("Run verification is missing, invalid or mismatched")
        if ground.get("designation")!="healthy" or ground.get("fault_category","missing") is not None:
            raise ValueError("Only independently verified healthy runs may fit the model")
        if "category" not in ground.get("config",{}) or ground["config"]["category"] is not None:
            raise ValueError("Faulty verification configuration is forbidden")
        if ground.get("simulation") is not (selection.purpose=="synthetic-test"):
            raise ValueError("Synthetic/research verification purpose mismatch")
        recovered = datetime.fromisoformat(ground["recovery_verified_at"].replace("Z","+00:00"))
        if recovered.tzinfo is None or recovered.utcoffset() is None:
            raise ValueError("Recovery verification must have an explicit timezone")
        run = prepare_directory(directory/"diagnostic",run_id=entry.run_id)
        source_hashes = {s.source:s.sha256 for s in run.ingestion_report.sources}
        fingerprint = tuple(sorted(source_hashes.items()))
        if fingerprint in fingerprints:
            raise ValueError("Duplicate diagnostic input content under different run identities")
        fingerprints.add(fingerprint)
        if ground.get("log_counts") != {"application":sum(f.source=="application.jsonl" for f in run.features),
                                       "postgresql":sum(f.source=="postgresql.jsonl" for f in run.features)}:
            raise ValueError("Verified diagnostic counts do not match complete ingestion")
        prepared.append(run)
        provenance.append(TrainingProvenance(run_id=entry.run_id,slot_id=assigned["slot_id"],diagnostic_directory=str(identity/"diagnostic"),
            source_sha256=source_hashes,verification_sha256=ground_hash,assignment_sha256=assignment_hash,
            feature_run_sha256=sha256(run.model_dump_json().encode())))
    return _fit_prepared(prepared,provenance=provenance,selection_sha256=selection_hash,
                         usage="synthetic-test-only" if selection.purpose=="synthetic-test" else "research",
                         config=config or DetectorConfig())
