"""LangGraph+real SDK path; defaults to mocked HTTP. Live requires explicit opt-in/model."""
import argparse
import asyncio
import hashlib
import json
import os
import stat
from importlib.metadata import version
from pathlib import Path
import sys

import httpx2
from logquant.detection.storage import write_files
from logquant.evidence.contracts import EvidencePayload, EvidenceReport
from logquant.thresholds.storage import read_bytes
from .contracts import ExecutionBudget
from .provider import MODEL, ConservativeCounter, OpenAIAdapter
from .transport_mock import HTTPFixture, MockHTTP
from .validation import prepare_evidence
from .workflow import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_directory",type=Path)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--mock-http",type=Path)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--candidate-id")
    parser.add_argument("--budget",type=Path)
    parser.add_argument("--overwrite",action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.live and (not args.model or args.mock_http):
            raise ValueError("Live mode requires explicit --model and forbids --mock-http")
        if not args.live and not args.mock_http:
            raise ValueError("Default offline mode requires --mock-http")
        model = args.model if args.model else MODEL
        if args.output_dir.resolve().is_relative_to(args.evidence_directory.resolve()):
            raise ValueError("Workflow outputs must stay outside evidence sources")
        for name in ("workflow.json","run-report.json"):
            if any((args.output_dir/name).resolve() == p.resolve() for p in (args.mock_http,args.budget) if p):
                raise ValueError("Outputs cannot overwrite input configuration/fixtures")
        inputs = {name:read_bytes(args.evidence_directory/name) for name in ("evidence.jsonl","evidence-report.json")}
        report = EvidenceReport.model_validate_json(inputs["evidence-report.json"])
        if report.input_usage != "synthetic-test-only" or report.status == "budget_failure":
            raise ValueError("This milestone CLI accepts only available synthetic smoke evidence")
        payloads = [EvidencePayload.model_validate_json(line) for line in inputs["evidence.jsonl"].splitlines()]
        if not payloads:
            if report.status != "no_candidates" or report.bundles: raise ValueError("No-candidate evidence mismatch")
            write_files(args.output_dir,{"run-report.json":b'{"status":"no_candidates","attempts":0}\n',"workflow.json":b'null\n'},overwrite=args.overwrite)
            return 0
        if args.candidate_id:
            selected = [p for p in payloads if p.candidate_id == args.candidate_id]
        else: selected = payloads if len(payloads)==1 else []
        if len(selected) != 1: raise ValueError("Select exactly one candidate with --candidate-id")
        payload = selected[0]
        audits = [a for a in report.bundles if a.candidate.candidate_id == payload.candidate_id]
        if len(audits) != 1 or report.run_id != payload.run_id: raise ValueError("Candidate audit/run mismatch")
        evidence = prepare_evidence(payload,audits[0])
        budget = ExecutionBudget()
        if args.budget:
            inputs["budget"] = read_bytes(args.budget);budget=ExecutionBudget.model_validate_json(inputs["budget"])
        counter = ConservativeCounter(model)
        handler = None
        if not args.live:
            inputs["mock-http"] = read_bytes(args.mock_http)
            fixture = HTTPFixture.model_validate_json(inputs["mock-http"])
            handler = MockHTTP(fixture.candidates[payload.candidate_id])
        # Detect existing output files before any future billable calls.
        from logquant.ingestion.boundary import directory_fd
        with directory_fd(args.output_dir,create=True) as output_fd:
            for name in ("workflow.json","run-report.json"):
                try: existing=os.stat(name,dir_fd=output_fd,follow_symlinks=False)
                except FileNotFoundError: continue
                if not stat.S_ISREG(existing.st_mode): raise ValueError("Workflow output must be a regular file")
                if not args.overwrite: raise FileExistsError("Workflow output exists; explicit --overwrite required")
        async def run():
            key = os.environ.get("OPENAI_API_KEY") if args.live else "mock-transport-only"
            if not key: raise ValueError("Live execution requires OPENAI_API_KEY in the environment")
            adapter = OpenAIAdapter(model=model,api_key=key,transport=httpx2.MockTransport(handler) if handler else None)
            try: return await execute(evidence,budget,counter,adapter,mode="live" if args.live else "mocked-transport")
            finally: await adapter.close()
        result = asyncio.run(run())
        summary = dict(status=result.status,mode=result.mode,model=model,candidate_id=payload.candidate_id,
            attempts=len(result.attempts),budget=budget.model_dump(mode="json"),
            counter=counter.identity,estimation_uncertainty="25% plus 1024 framing reserve is heuristic; provider usage is authoritative",
            input_sha256={name:hashlib.sha256(data).hexdigest() for name,data in inputs.items()},
            mock_http_calls=len(handler.calls) if handler else None,graph_version="triage-rca-graph-v1",
            dependency_versions={name:version(name) for name in ("openai","langgraph","tiktoken")},
            analysis_lock_sha256=hashlib.sha256((Path(__file__).resolve().parents[3]/"requirements-analysis.lock").read_bytes()).hexdigest())
        write_files(args.output_dir,{"workflow.json":(result.model_dump_json(indent=2)+"\n").encode(),
            "run-report.json":(json.dumps(summary,indent=2)+"\n").encode()},overwrite=args.overwrite)
        print(json.dumps({"mode":result.mode,"status":result.status,"attempts":len(result.attempts)}))
        return 0 if result.status == "validated" else 3
    except (ValueError,OSError,TypeError,KeyError) as exc:
        print(f"Workflow rejected: {exc}",file=sys.stderr);return 1
    return 0
