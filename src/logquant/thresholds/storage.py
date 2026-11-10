"""JSON threshold persistence. Never loads evaluation metadata."""
import json
import os
from pathlib import Path

from logquant.ingestion.boundary import directory_fd, open_source
from logquant.detection.core import DetectorBundle, sha256
from logquant.detection.storage import load_trusted_artifact, write_files
from .contracts import CalibrationArtifact, DetectorIdentity


def read_bytes(path: Path) -> bytes:
    with directory_fd(path.parent) as directory:
        with os.fdopen(open_source(directory, path.name), "rb") as stream:
            return stream.read()


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def seal(payload: dict) -> CalibrationArtifact:
    payload = dict(payload)
    payload.pop("artifact_id", None)
    # Materialize defaults before computing content identity.
    provisional = CalibrationArtifact.model_validate_json(json.dumps({**payload, "artifact_id": "pending"}))
    data = provisional.model_dump(mode="json", exclude={"artifact_id"})
    return provisional.model_copy(update={"artifact_id": sha256(canonical(data))})


def validate_artifact(artifact: CalibrationArtifact) -> CalibrationArtifact:
    value = CalibrationArtifact.model_validate_json(artifact.model_dump_json())
    if seal(value.model_dump(mode="json")).artifact_id != value.artifact_id:
        raise ValueError("Threshold artifact content checksum mismatch")
    return value


def save_threshold(artifact: CalibrationArtifact, directory: Path, *, overwrite: bool = False) -> None:
    artifact = validate_artifact(artifact)
    for item in artifact.inputs:
        if any(directory.resolve().is_relative_to(p.resolve()) for p in
               (Path(item.directory), Path(item.score_directory))):
            raise ValueError("Calibration outputs must stay outside run and score sources")
    write_files(directory, {"calibration.json": (artifact.model_dump_json(indent=2)+"\n").encode()}, overwrite=overwrite)


def load_threshold(directory: Path) -> CalibrationArtifact:
    return validate_artifact(CalibrationArtifact.model_validate_json(read_bytes(directory/"calibration.json")))


def load_detector(directory: Path) -> tuple[DetectorBundle, DetectorIdentity]:
    """Load trusted project pickle and obtain its verified exact payload identity."""
    bundle = load_trusted_artifact(directory)
    envelope = json.loads(read_bytes(directory/"model-metadata.json"))
    if envelope["metadata"] != bundle.metadata.model_dump(mode="json"):
        raise ValueError("Detector metadata changed during load")
    digest = sha256(read_bytes(directory/"model.pkl"))
    if digest != envelope["pickle_sha256"]:
        raise ValueError("Detector payload changed during load")
    return bundle, DetectorIdentity(model_id=bundle.metadata.model_id, usage=bundle.metadata.usage, payload_sha256=digest)
