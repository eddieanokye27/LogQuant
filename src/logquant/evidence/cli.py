"""Select offline bounded original evidence from one run and frozen alert exports."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from logquant.ingestion import IngestionError, load_diagnostic_directory
from logquant.thresholds.contracts import AlertResult
from logquant.thresholds.storage import read_bytes
from logquant.detection.storage import write_files
from .contracts import EvidenceConfig
from .selection import payload_bytes, select_evidence


ALERT_FILES = ("alerts.jsonl", "candidates.jsonl", "alert-report.json")


def _pairs(items: list[tuple]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key in evidence configuration/alert input")
        result[key] = value
    return result


def _json(data: bytes) -> dict:
    return json.loads(data, object_pairs_hook=_pairs)


def load_alert_export(directory: Path) -> tuple[AlertResult, dict[str, str], dict[str, str]]:
    files = {name: read_bytes(directory/name) for name in ALERT_FILES}
    report = _json(files["alert-report.json"])
    windows = [_json(line) for line in files["alerts.jsonl"].splitlines()]
    candidates = [_json(line) for line in files["candidates.jsonl"].splitlines()]
    value = {key: report[key] for key in ("detector", "threshold", "policy", "score_definition", "coverage")}
    value.update(windows=windows, candidates=candidates)
    alerts = AlertResult.model_validate_json(json.dumps(value))
    if report["candidates"] != len(candidates):
        raise ValueError("Alert report candidate count differs from input")
    hashes = report["input_sha256"]
    if not isinstance(hashes, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in hashes.items()):
        raise ValueError("Missing or invalid alert diagnostic fingerprints")
    return alerts, hashes, {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("diagnostic_directory", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--alerts-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if any(args.output_dir.resolve().is_relative_to(p.resolve()) for p in (args.diagnostic_directory, args.alerts_dir)):
            raise ValueError("Evidence outputs must stay outside diagnostic and alert source directories")
        config = EvidenceConfig.model_validate(_json(read_bytes(args.config))) if args.config else EvidenceConfig()
        alerts, diagnostic_hashes, alert_hashes = load_alert_export(args.alerts_dir)
        result = select_evidence(load_diagnostic_directory(args.diagnostic_directory), run_id=args.run_id,
            alerts=alerts, diagnostic_sha256=diagnostic_hashes, config=config, alert_input_sha256=alert_hashes)
        write_files(args.output_dir, {"evidence.jsonl": b"".join(payload_bytes(p) for p in result.payloads),
            "evidence-report.json": (result.report.model_dump_json(indent=2)+"\n").encode("utf-8")}, overwrite=args.overwrite)
        print(json.dumps(dict(status=result.report.status, candidates=len(result.report.bundles),
                             payloads=len(result.payloads), usage=result.report.input_usage)))
        return 3 if result.report.status == "budget_failure" else 0
    except IngestionError as exc:
        print(exc.result.report.model_dump_json(indent=2), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Evidence selection failed: {exc}", file=sys.stderr)
        return 1
    return 0
