"""Strict single-run feature CLI with separate no-clobber artifacts."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from logquant.ingestion import IngestionError
from logquant.ingestion.boundary import directory_fd
from .models import FeatureRun
from .windowing import prepare_directory


def write_outputs(run: FeatureRun, source: Path, output: Path, *, overwrite: bool = False) -> None:
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Output must be outside the diagnostic source directory")
    report = run.model_dump(mode="json", exclude={"features", "windows"})
    report.update(record_count=len(run.features), window_count=len(run.windows),
                  empty_window_count=sum(w.empty for w in run.windows), complete=True,
                  leading_trailing_collection_periods="unknown; not inferred")
    contents = {"features.jsonl": "".join(r.model_dump_json()+"\n" for r in run.features),
                "windows.jsonl": "".join(w.model_dump_json()+"\n" for w in run.windows),
                "feature-report.json": json.dumps(report, ensure_ascii=False, indent=2)+"\n"}
    with directory_fd(output, create=True) as directory:
        for name in contents:
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Output must be a regular file: {name}")
            if not overwrite:
                raise FileExistsError(f"Output exists: {name}; use --overwrite explicitly")
        pending: dict[str, str] = {}
        created: list[str] = []
        try:
            for name, content in contents.items():
                temporary = f".logquant-{uuid.uuid4().hex}.tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                pending[name] = temporary
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
            for name, temporary in pending.items():
                if overwrite:
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                else:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    created.append(name)
        except BaseException:
            for name in created:
                os.unlink(name, dir_fd=directory)
            raise
        finally:
            for temporary in pending.values():
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build feature-only text and observed-event 30-second windows for one run")
    parser.add_argument("diagnostic_directory", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        run = prepare_directory(args.diagnostic_directory, run_id=args.run_id)
        write_outputs(run, args.diagnostic_directory, args.output_dir, overwrite=args.overwrite)
    except IngestionError as exc:
        print(exc.result.report.model_dump_json(indent=2), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"Feature preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"records": len(run.features), "windows": len(run.windows),
                      "empty_windows": sum(w.empty for w in run.windows)}))
    return 0
