"""Create TEST-ONLY canned responses for a supplied synthetic evidence directory.

Responses test contract validation, not diagnosis accuracy. No model is called.
"""
import argparse
import json
from pathlib import Path

from logquant.agents.cli import MockFixture
from logquant.agents.mock import MockReply, MockResponses
from logquant.evidence.contracts import EvidencePayload, EvidenceReport


def example_responses(payload: EvidencePayload) -> tuple[dict, dict]:
    chosen = next(e for e in payload.evidence if e.region == "candidate")
    citation = chosen.citation.model_dump(mode="json")
    triage = dict(schema_version=1,
        symptoms=[dict(text="The cited record reports an operation outcome.", citations=[citation])],
        affected_services=[dict(service=chosen.record.service, description="This service emitted the cited record.", citations=[citation])],
        timeline=[dict(timestamp=chosen.record.timestamp.isoformat(), description="The cited record was emitted.", citations=[citation])],
        uncertainties=["These mocked statements do not establish a cause."],
        evidence_limitations=["Only bounded retained offline observations are available."])
    rca = dict(schema_version=1, status="insufficient_evidence",
        summary="This canned response abstains; it is not a diagnosis assessment.",
        observations=[dict(text="A record is present in the supplied candidate evidence.", citations=[citation])],
        hypotheses=[], uncertainties=["No causal determination is attempted by this mock."],
        evidence_limitations=["Omitted or unobserved records cannot be assessed."])
    return triage, rca


def create_responses(evidence: Path, output: Path) -> None:
    report = EvidenceReport.model_validate_json((evidence/"evidence-report.json").read_bytes())
    if report.input_usage != "synthetic-test-only": raise ValueError("Only synthetic evidence is permitted")
    payloads = [EvidencePayload.model_validate_json(line) for line in (evidence/"evidence.jsonl").read_bytes().splitlines()]
    candidates = {}
    for payload in payloads:
        triage, rca = example_responses(payload)
        candidates[payload.candidate_id] = MockResponses(triage=(MockReply(output=json.dumps(triage)),),
                                                       rca=(MockReply(output=json.dumps(rca)),))
    fixture = MockFixture(schema_version=1, purpose="synthetic-test-only", candidates=candidates)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream: stream.write(fixture.model_dump_json(indent=2)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    create_responses(args.evidence_directory, args.output)
