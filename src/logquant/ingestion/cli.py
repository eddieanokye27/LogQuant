"""Write ingestion artifacts outside the source, with explicit no-clobber defaults."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from .boundary import directory_fd
from .models import IngestionError, IngestionResult
from .parser import load_diagnostic_directory


def write_outputs(result: IngestionResult, source: Path, output: Path, *, overwrite: bool = False) -> None:
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Output directory must be outside the diagnostic source directory")
    # No normalized partial artifact in strict mode; the separate report explains failure.
    contents = {"ingestion-report.json": result.report.model_dump_json(indent=2)+"\n"}
    if result.complete or result.report.mode == "permissive":
        contents["records.jsonl"] = "".join(r.model_dump_json()+"\n" for r in result.records)
    with directory_fd(output, create=True) as directory:
        # Check both reserved names, even if this attempt only writes an error report.
        for name in ("records.jsonl", "ingestion-report.json"):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Output is not a regular file: {name}")
            if not overwrite:
                raise FileExistsError(f"Output already exists: {name}; use --overwrite explicitly")
        if not result.complete and result.report.mode == "strict":
            # Do not leave a previous normalized artifact beside a new strict failure report.
            try:
                os.unlink("records.jsonl", dir_fd=directory)
            except FileNotFoundError:
                pass
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
    parser = argparse.ArgumentParser(description="Parse only application.jsonl and postgresql.jsonl in one diagnostic directory")
    parser.add_argument("diagnostic_directory", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--permissive", action="store_true", help="Write valid records and errors, mark incomplete, exit 2")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace existing ingestion artifacts")
    args = parser.parse_args(argv)
    try:
        try:
            result = load_diagnostic_directory(args.diagnostic_directory, permissive=args.permissive)
        except IngestionError as exc:
            result = exc.result
        write_outputs(result, args.diagnostic_directory, args.output_dir, overwrite=args.overwrite)
    except (OSError, ValueError) as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"complete": result.complete, "records": len(result.records), "errors": len(result.report.errors)}))
    return 0 if result.complete else 2
