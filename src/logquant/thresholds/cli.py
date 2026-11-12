"""Development-only calibration and ground-truth-free frozen alert classification."""
import argparse
import json
from pathlib import Path
import sys

from logquant.detection.core import score_run
from logquant.detection.storage import write_files
from logquant.features import prepare_directory
from logquant.ingestion import IngestionError
from .alerts import apply_threshold
from .contracts import CalibrationConfig
from .storage import load_detector, load_threshold, read_bytes, save_threshold


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("manifest", type=Path)
    calibrate.add_argument("--config", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("diagnostic_directory", type=Path)
    apply.add_argument("--run-id", required=True)
    apply.add_argument("--threshold-dir", type=Path, required=True)
    for command in (calibrate, apply):
        command.add_argument('--policy',choices=['verified-interval-v1','whole-run-v2'],default='verified-interval-v1')
        command.add_argument("--detector-dir", type=Path, required=True, help="Trusted project model pickle only")
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        from . import whole_run
        whole=args.policy=='whole-run-v2'
        bundle, identity = load_detector(args.detector_dir)
        protected = [args.detector_dir, *(Path(p.diagnostic_directory) for p in bundle.metadata.training_provenance)]
        if args.command == "apply": protected.extend([args.diagnostic_directory, args.threshold_dir])
        if any(args.output_dir.resolve().is_relative_to(p.resolve()) for p in protected):
            raise ValueError("Outputs must stay outside model, threshold and diagnostic sources")
        if args.command == "calibrate":
            from .orchestration import calibrate_manifest
            config = (whole_run.WholeRunConfig if whole else CalibrationConfig).model_validate_json(read_bytes(args.config))
            artifact = calibrate_manifest(args.manifest, bundle, identity, config)
            if any(args.output_dir.resolve().is_relative_to(Path(i.directory).resolve()) for i in artifact.inputs):
                raise ValueError("Calibration outputs must stay outside selected run directories")
            (whole_run.save if whole else save_threshold)(artifact,args.output_dir,overwrite=args.overwrite)
            print(json.dumps(dict(status=artifact.status, threshold=artifact.threshold, usage=identity.usage, reasons=artifact.reasons)))
            return 0 if artifact.status == "selected" else 3
        artifact = (whole_run.load if whole else load_threshold)(args.threshold_dir)
        if any(args.output_dir.resolve().is_relative_to(p.resolve()) for item in artifact.inputs
               for p in (Path(item.directory)/"diagnostic", Path(item.score_directory))):
            raise ValueError("Alert outputs must stay outside calibration diagnostic/score sources")
        # Check compatibility before reading diagnostic data.
        if artifact.detector != identity or artifact.status != "selected":
            raise ValueError("Incompatible or unsuccessful threshold artifact")
        run = prepare_directory(args.diagnostic_directory, run_id=args.run_id)
        result = (whole_run.apply if whole else apply_threshold)(score_run(bundle,run),identity,artifact)
        report = result.model_dump(mode="json", exclude={"windows", "candidates"})
        report.update(threshold_artifact_id=artifact.artifact_id, candidates=len(result.candidates),
                      threshold_calibration_policy=artifact.policy,
                      input_sha256={s.source: s.sha256 for s in run.ingestion_report.sources})
        if whole:
            chosen=whole_run.first_candidate(result)
            report['selected_candidate_id']=chosen.candidate_id if chosen else None
        write_files(args.output_dir, {
            "alerts.jsonl": "".join(w.model_dump_json()+"\n" for w in result.windows).encode(),
            "candidates.jsonl": "".join(c.model_dump_json()+"\n" for c in result.candidates).encode(),
            "alert-report.json": (json.dumps(report, indent=2)+"\n").encode()}, overwrite=args.overwrite)
        print(json.dumps(dict(windows=len(result.windows), candidates=len(result.candidates), usage=identity.usage)))
    except IngestionError as exc:
        print(exc.result.report.model_dump_json(indent=2), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Threshold operation failed: {exc}", file=sys.stderr)
        return 1
    return 0
