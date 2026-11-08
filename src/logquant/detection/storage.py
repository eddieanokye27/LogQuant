"""Trusted-source-only pickle persistence, integrity checks and no-clobber outputs."""
import json
import os
from pathlib import Path
import pickle
import stat
import uuid

from logquant.ingestion.boundary import directory_fd, open_source
from .contracts import ModelMetadata
from .core import DetectorBundle, dependency_versions, implementation_sha256, sha256, ROOT


def write_files(output: Path, contents: dict[str, bytes], *, overwrite: bool = False) -> None:
    with directory_fd(output,create=True) as directory:
        for name in contents:
            try: info=os.stat(name,dir_fd=directory,follow_symlinks=False)
            except FileNotFoundError: continue
            if not stat.S_ISREG(info.st_mode): raise ValueError(f"Output is not a regular file: {name}")
            if not overwrite: raise FileExistsError(f"Output exists: {name}; explicit --overwrite is required")
        pending,created = {},[]
        try:
            for name,data in contents.items():
                temporary=f".logquant-{uuid.uuid4().hex}.tmp"
                fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=directory)
                pending[name]=temporary
                with os.fdopen(fd,"wb") as stream:stream.write(data)
            for name,temporary in pending.items():
                if overwrite:os.replace(temporary,name,src_dir_fd=directory,dst_dir_fd=directory)
                else:
                    os.link(temporary,name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
                    created.append(name)
        except BaseException:
            for name in created:os.unlink(name,dir_fd=directory)
            raise
        finally:
            for name in pending.values():
                try:os.unlink(name,dir_fd=directory)
                except FileNotFoundError:pass


def save_artifact(bundle: DetectorBundle, path: str | Path, *, overwrite: bool = False) -> None:
    path=Path(path)
    for run in bundle.metadata.training_provenance:
        if path.resolve().is_relative_to(Path(run.diagnostic_directory).resolve()):
            raise ValueError("Artifact destination must be outside training diagnostic sources")
    payload=pickle.dumps(bundle,protocol=5)
    envelope={"metadata":bundle.metadata.model_dump(mode="json"),"pickle_sha256":sha256(payload)}
    write_files(path,{"model.pkl":payload,"model-metadata.json":(json.dumps(envelope,indent=2)+"\n").encode()},overwrite=overwrite)


def load_trusted_artifact(path: str | Path) -> DetectorBundle:
    """ONLY call for trusted project artifacts. Pickle can execute arbitrary code.

    A matching checksum does not authenticate an untrusted artifact or make it safe.
    """
    with directory_fd(Path(path)) as directory:
        with os.fdopen(open_source(directory,"model-metadata.json"),"rb") as stream:
            envelope=json.load(stream)
        expected=ModelMetadata.model_validate_json(json.dumps(envelope["metadata"]))
        if expected.dependency_versions!=dependency_versions():
            raise ValueError("Model dependency/Python/platform versions differ from the active environment")
        if expected.analysis_lock_sha256!=sha256((ROOT/"requirements-analysis.lock").read_bytes()):
            raise ValueError("Model analysis dependency lock differs from the active lock")
        if expected.implementation_sha256!=implementation_sha256():
            raise ValueError("Model preprocessing/detector implementation fingerprint differs")
        with os.fdopen(open_source(directory,"model.pkl"),"rb") as stream:
            payload=stream.read()
    if sha256(payload)!=envelope["pickle_sha256"]:raise ValueError("Model payload checksum mismatch")
    bundle=pickle.loads(payload)  # Trusted input only; never accept model uploads.
    if not isinstance(bundle,DetectorBundle) or bundle.metadata!=expected:
        raise ValueError("Model payload and metadata do not match")
    return bundle
