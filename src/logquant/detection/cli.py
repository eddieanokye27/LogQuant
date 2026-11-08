"""Offline fitting/scoring; no threshold, alert classification or evaluation metrics."""
import argparse
import json
from pathlib import Path
import sys

from logquant.features import prepare_directory
from logquant.ingestion import IngestionError
from .contracts import DetectorConfig
from .core import score_run
from .storage import load_trusted_artifact, save_artifact, write_files


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    fit=sub.add_parser("fit",help="Only verified healthy whole training runs in a selection manifest")
    fit.add_argument("manifest",type=Path)
    fit.add_argument("--artifact-dir",type=Path,required=True)
    fit.add_argument("--config",type=Path)
    fit.add_argument("--overwrite",action="store_true")
    score=sub.add_parser("score",help="Load a TRUSTED project pickle only; never load uploaded/untrusted models")
    score.add_argument("diagnostic_directory",type=Path)
    score.add_argument("--run-id",required=True)
    score.add_argument("--artifact-dir",type=Path,required=True)
    score.add_argument("--output-dir",type=Path,required=True)
    score.add_argument("--overwrite",action="store_true")
    args=parser.parse_args(argv)
    try:
        if args.command=="fit":
            from logquant.training import fit_manifest
            config=DetectorConfig.model_validate_json(args.config.read_bytes()) if args.config else DetectorConfig()
            bundle=fit_manifest(args.manifest,config=config)
            save_artifact(bundle,args.artifact_dir,overwrite=args.overwrite)
            print(json.dumps({"model_id":bundle.metadata.model_id,"usage":bundle.metadata.usage,
                              "fit_windows":bundle.metadata.detector_fit_windows,"alert_threshold":None}))
        else:
            if any(args.output_dir.resolve().is_relative_to(p.resolve()) for p in (args.diagnostic_directory,args.artifact_dir)):
                raise ValueError("Score outputs must be outside diagnostics and the model artifact directory")
            bundle=load_trusted_artifact(args.artifact_dir)
            if any(args.output_dir.resolve().is_relative_to(Path(p.diagnostic_directory).resolve())
                   for p in bundle.metadata.training_provenance):
                raise ValueError("Score outputs must also be outside training diagnostic sources")
            run=prepare_directory(args.diagnostic_directory,run_id=args.run_id)
            result=score_run(bundle,run)
            report=result.model_dump(mode="json",exclude={"scores"})
            report.update(window_count=len(result.scores),scored=sum(s.status=="scored" for s in result.scores),
                empty_documents=sum(s.status=="empty_document" for s in result.scores),
                zero_vectors=sum(s.status=="zero_vector" for s in result.scores),
                coverage_unknown=sum(s.coverage_unknown for s in result.scores))
            write_files(args.output_dir,{"scores.jsonl":"".join(s.model_dump_json()+"\n" for s in result.scores).encode(),
                "score-report.json":(json.dumps(report,indent=2)+"\n").encode()},overwrite=args.overwrite)
            print(json.dumps({"windows":len(result.scores),"scored":report["scored"],"usage":bundle.metadata.usage,"alert_threshold":None}))
    except IngestionError as exc:
        print(exc.result.report.model_dump_json(indent=2),file=sys.stderr)
        return 2
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f"Detector operation failed: {exc}",file=sys.stderr)
        return 1
    return 0
