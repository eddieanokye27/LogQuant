"""Offline mock attempts only. No transport, SDK, sleeping, or LangGraph execution."""
from typing import Literal
from pydantic import Field, ValidationError

from logquant.ingestion.models import Contract
from .contracts import ExecutionBudget, RCAResponse, TriageResponse
from .prompts import AgentRequest, TokenCounter, build_request, count_text, preflight
from .validation import AgentEvidence, validate_response


class MockReply(Contract):
    output: str
    elapsed_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)


class MockResponses(Contract):
    triage: tuple[MockReply, ...]
    rca: tuple[MockReply, ...]


class Attempt(Contract):
    stage: Literal["triage", "rca"]
    number: int
    request: AgentRequest
    raw_output: str
    output_count: int
    simulated_elapsed_seconds: float
    status: Literal["validated", "invalid_output", "timeout"]
    errors: tuple[str, ...]


class MockWorkflowResult(Contract):
    mode: Literal["mock-only-no-model-calls"] = "mock-only-no-model-calls"
    status: Literal["validated", "processing_failure"]
    triage: TriageResponse | None
    rca: RCAResponse | None
    attempts: tuple[Attempt, ...]
    errors: tuple[str, ...]
    agents_repaired: tuple[str, ...]
    max_calls_per_candidate: int
    mock_calls_consumed: int
    total_simulated_elapsed_seconds: float
    accounting_note: Literal["Mock counts/durations are not billed usage or measured latency."] = "Mock counts/durations are not billed usage or measured latency."


def run_mock(evidence: AgentEvidence, responses: MockResponses, budget: ExecutionBudget,
             counter: TokenCounter) -> MockWorkflowResult:
    budget = ExecutionBudget.model_validate_json(budget.model_dump_json())
    attempts: list[Attempt] = []
    repaired: list[str] = []
    errors: list[str] = []
    triage, rca = None, None
    try:
        preflight(evidence, budget, counter)
        for stage, replies, schema in (("triage", responses.triage, TriageResponse), ("rca", responses.rca, RCAResponse)):
            previous, reasons = None, ()
            validated = None
            for index in range(1+budget.max_repair_attempts):
                request = build_request(stage, evidence, budget, counter, triage=triage if stage == "rca" else None,
                                        previous_output=previous, errors=reasons)
                if index >= len(replies):
                    raise ValueError(f"{stage}: mock replies exhausted; no valid response")
                reply = replies[index]
                output_count = count_text(counter, reply.output)
                failure = []
                timed_out = reply.elapsed_seconds > budget.timeout_seconds
                if timed_out: failure.append("Supplied mock elapsed time exceeds per-call timeout")
                elif output_count > request.max_output_tokens:
                    failure.append("Mock output exceeds the configured output allowance")
                else:
                    try:
                        validated = validate_response(reply.output, schema, evidence)
                    except ValidationError as exc:
                        failure.extend(f"{'.'.join(map(str,e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False))
                    except (ValueError, TypeError) as exc:
                        failure.append(str(exc))
                attempts.append(Attempt(stage=stage, number=index+1, request=request, raw_output=reply.output,
                    output_count=output_count, simulated_elapsed_seconds=reply.elapsed_seconds,
                    status="timeout" if timed_out else "invalid_output" if failure else "validated", errors=tuple(failure)))
                if not failure:
                    if index: repaired.append(stage)
                    break
                if timed_out: raise ValueError(f"{stage}: timeout; transport retries are disabled")
                previous, reasons = reply.output, tuple(failure)
            if validated is None:
                raise ValueError(f"{stage}: structured-output repair allowance exhausted")
            if stage == "triage": triage = validated
            else: rca = validated
    except (ValueError, TypeError) as exc:
        errors.append(str(exc))
    return MockWorkflowResult(status="validated" if rca is not None else "processing_failure", triage=triage, rca=rca,
        attempts=tuple(attempts), errors=tuple(errors), agents_repaired=tuple(repaired),
        max_calls_per_candidate=2*(1+budget.max_repair_attempts), mock_calls_consumed=len(attempts),
        total_simulated_elapsed_seconds=sum(a.simulated_elapsed_seconds for a in attempts))
