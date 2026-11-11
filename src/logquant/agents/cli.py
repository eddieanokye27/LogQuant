"""Offline/mock demonstration only. No provider SDK, model calls or LangGraph."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Literal

from logquant.detection.storage import write_files
from logquant.evidence.contracts import EvidencePayload, EvidenceReport
from logquant.ingestion.models import Contract
from logquant.thresholds.storage import read_bytes
from .contracts import ExecutionBudget
from .mock import MockResponses, run_mock
from .prompts import MockLexicalCounter
from .validation import prepare_evidence


class MockFixture(Contract):
    schema_version: Literal[1]
    purpose: Literal["synthetic-test-only"]
    candidates: dict[str, MockResponses]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_directory", type=Path)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.output_dir.resolve().is_relative_to(args.evidence_directory.resolve()):
            raise ValueError("Mock outputs must stay outside the evidence source directory")
        names = ("mock-results.jsonl", "validated-responses.jsonl", "mock-report.json")
        if any((args.output_dir/name).resolve() == p.resolve() for name in names
               for p in (args.responses, args.budget) if p is not None):
            raise ValueError("Cannot overwrite mock response/budget inputs")
        inputs = {"evidence.jsonl": read_bytes(args.evidence_directory/"evidence.jsonl"),
                  "evidence-report.json": read_bytes(args.evidence_directory/"evidence-report.json"),
                  "mock_responses": read_bytes(args.responses)}
        fixture = MockFixture.model_validate_json(inputs["mock_responses"])
        report = EvidenceReport.model_validate_json(inputs["evidence-report.json"])
        if report.input_usage != "synthetic-test-only" or report.status == "budget_failure":
            raise ValueError("Mock CLI requires available synthetic-only evidence; budget failures must be resolved first")
        payloads = [EvidencePayload.model_validate_json(line) for line in inputs["evidence.jsonl"].splitlines()]
        ids = [p.candidate_id for p in payloads]
        if len(set(ids)) != len(ids) or set(ids) != set(fixture.candidates):
            raise ValueError("Mock candidate identities must match the exact supplied payloads")
        audits = {a.candidate.candidate_id: a for a in report.bundles}
        if len(audits) != len(report.bundles) or set(audits) != set(ids):
            raise ValueError("Evidence audit/payload candidate membership mismatch")
        if (report.status == "no_candidates") != (not payloads):
            raise ValueError("Evidence no-candidate status mismatch")
        budget = ExecutionBudget()
        if args.budget:
            inputs["budget"] = read_bytes(args.budget)
            budget = ExecutionBudget.model_validate_json(inputs["budget"])
        counter = MockLexicalCounter()
        rows, validated = [], []
        for payload in sorted(payloads, key=lambda p: (p.candidate_start, p.candidate_id)):
            if payload.run_id != report.run_id: raise ValueError("Cross-run evidence payload")
            context = prepare_evidence(payload, audits[payload.candidate_id])
            result = run_mock(context, fixture.candidates[payload.candidate_id], budget, counter)
            rows.append(dict(candidate_id=payload.candidate_id, run_id=payload.run_id, result=result.model_dump(mode="json")))
            if result.status == "validated":
                validated.append(dict(candidate_id=payload.candidate_id, run_id=payload.run_id,
                    validation_only=True, triage=result.triage.model_dump(mode="json"), rca=result.rca.model_dump(mode="json")))
        failures = sum(r["result"]["status"] == "processing_failure" for r in rows)
        summary = dict(mode="mock-only-no-model-calls", status="no_candidates" if not rows else "processing_failure" if failures else "validated",
            candidates=len(rows), validated=len(validated), processing_failures=failures,
            mock_calls_consumed=sum(r["result"]["mock_calls_consumed"] for r in rows),
            budget=budget.model_dump(mode="json"), counter_identity=counter.identity, accounting_unit=counter.unit,
            input_sha256={name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()},
            note="Structural/citation validation only; no diagnosis accuracy, model tokens, billed usage or measured model latency.")
        write_files(args.output_dir, {
            "mock-results.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True)+"\n" for r in rows).encode(),
            "validated-responses.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True)+"\n" for r in validated).encode(),
            "mock-report.json": (json.dumps(summary, indent=2)+"\n").encode()}, overwrite=args.overwrite)
        print(json.dumps(dict(status=summary["status"], candidates=len(rows), mock_calls=summary["mock_calls_consumed"], live_calls=0)))
        return 3 if failures else 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(f"Offline agent contract check failed: {exc}", file=sys.stderr)
        return 1
    return 0
