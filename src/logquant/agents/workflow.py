"""Minimal explicit LangGraph execution; only graph validation routes can repair."""
import asyncio
import hashlib
from time import perf_counter
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph, START, END
from langsmith import tracing_context
from pydantic import ValidationError

from logquant.ingestion.models import Contract
from .contracts import ExecutionBudget, RCAResponse, TriageResponse
from .prompts import AgentRequest, TokenCounter, build_request, preflight, canonical
from .provider import MODEL, OpenAIAdapter, ProviderOutcome
from .validation import AgentEvidence, validate_response


class GenerationAttempt(Contract):
    stage: Literal["triage","rca"]
    number: int
    client_request_id: str
    request: AgentRequest
    schema_version: str
    wire_schema_sha256: str
    outcome: ProviderOutcome
    validation_errors: tuple[str,...] = ()
    validated: bool = False
    input_count_is_estimate: Literal[True] = True


class WorkflowResult(Contract):
    mode: Literal["mocked-transport","live"]
    model: str
    status: Literal["validated","processing_failure","cancelled"]
    terminal_reason: str | None
    triage: TriageResponse | None
    rca: RCAResponse | None
    attempts: tuple[GenerationAttempt,...]
    route_trace: tuple[str,...]
    max_generation_attempts: int
    elapsed_seconds: float
    known_input_tokens: int
    known_output_tokens: int
    total_input_tokens: int | None
    total_output_tokens: int | None
    unknown_usage_attempts: int
    usage_is_mocked: bool
    estimated_cost_usd: None = None
    billing_note: str = "Unknown usage stays unknown; local timeout/cancellation does not prove provider processing or billing stopped."


class AgentState(TypedDict, total=False):
    triage: TriageResponse | None
    rca: RCAResponse | None
    request: AgentRequest
    attempts: list[GenerationAttempt]
    next: str
    terminal_reason: str | None
    status: str


async def execute(evidence: AgentEvidence, budget: ExecutionBudget, counter: TokenCounter,
                  adapter: OpenAIAdapter, *, mode: Literal["mocked-transport","live"]) -> WorkflowResult:
    budget = ExecutionBudget.model_validate_json(budget.model_dump_json())
    if adapter.model != MODEL or budget.context_limit_tokens > 400000 or max(budget.triage_output_tokens,budget.rca_output_tokens) > 128000:
        raise ValueError("Budget/model exceeds the supported profile")
    if mode == "live" and not counter.identity.startswith(MODEL+":"):
        raise ValueError("Live execution requires the model-specific conservative counter")
    started = perf_counter()
    journal: list[GenerationAttempt] = []
    trace: list[str] = []
    validated = {"triage":None,"rca":None}
    maximum = 2*(1+budget.max_repair_attempts)

    def admission(state):
        trace.append("preflight")
        try: preflight(evidence,budget,counter)
        except (ValueError,TypeError) as exc:
            return {"next":"failure","terminal_reason":str(exc)}
        return {"next":"triage_prepare"}

    def prepare(stage, repair):
        def node(state):
            trace.append(stage+ ("_repair" if repair else "_prepare"))
            previous = journal[-1] if repair else None
            try:
                request = build_request(stage,evidence,budget,counter,triage=state.get("triage") if stage == "rca" else None,
                    previous_output=previous.outcome.raw_output if previous else None,
                    errors=previous.validation_errors if previous else ())
            except (ValueError,TypeError) as exc:
                return {"next":"failure","terminal_reason":str(exc)}
            return {"request":request,"next":stage+"_generate"}
        return node

    def generate(stage):
        async def node(state):
            trace.append(stage+"_generate")
            number = sum(a.stage == stage for a in journal)+1
            if len(journal) >= maximum or number > 1+budget.max_repair_attempts:
                return {"next":"failure","terminal_reason":"Generation attempt cap exceeded"}
            client_id = str(uuid4())
            request = state["request"]
            from .provider import strict_schema
            outcome = await adapter.generate(request,client_id)
            attempt = GenerationAttempt(stage=stage,number=number,client_request_id=client_id,request=request,
                schema_version=stage+"-response-v1",wire_schema_sha256=hashlib.sha256(canonical(strict_schema(request.body.response_schema)).encode()).hexdigest(),
                outcome=outcome)
            journal.append(attempt)
            return {"attempts":list(journal),"next":stage+"_validate"}
        return node

    def validation(stage):
        def node(state):
            trace.append(stage+"_validate")
            attempt = journal[-1]; outcome = attempt.outcome
            if outcome.status != "completed":
                return {"next":"failure","terminal_reason":outcome.status}
            if (outcome.usage.input_tokens is not None and outcome.usage.input_tokens > attempt.request.input_count):
                return {"next":"failure","terminal_reason":"Provider input usage exceeded conservative estimate; requalification required"}
            if outcome.usage.output_tokens is not None and outcome.usage.output_tokens > attempt.request.max_output_tokens:
                return {"next":"failure","terminal_reason":"Provider output cap violation"}
            try:
                response = validate_response(outcome.raw_output, TriageResponse if stage == "triage" else RCAResponse,evidence)
            except ValidationError as exc:
                errors = tuple(f"{'.'.join(map(str,e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False))
            except (ValueError,TypeError) as exc:
                errors = (str(exc),)
            else:
                journal[-1] = attempt.model_copy(update={"validated":True})
                validated[stage] = response
                return {stage:response,"attempts":list(journal),"next":"rca_prepare" if stage == "triage" else "completion"}
            journal[-1] = attempt.model_copy(update={"validation_errors":errors})
            can_repair = attempt.number <= budget.max_repair_attempts
            return {"attempts":list(journal),"next":stage+"_repair" if can_repair else "failure",
                    "terminal_reason":None if can_repair else stage+": validation/repair allowance exhausted"}
        return node

    def complete(state):
        trace.append("completion")
        return {"status":"validated","terminal_reason":None}

    def failure(state):
        trace.append("failure")
        return {"status":"cancelled" if state.get("terminal_reason")=="cancelled" else "processing_failure"}

    graph = StateGraph(AgentState)
    graph.add_node("preflight",admission)
    for stage in ("triage","rca"):
        graph.add_node(stage+"_prepare",prepare(stage,False))
        graph.add_node(stage+"_generate",generate(stage))
        graph.add_node(stage+"_validate",validation(stage))
        graph.add_node(stage+"_repair",prepare(stage,True))
    graph.add_node("completion",complete); graph.add_node("failure",failure)
    graph.add_edge(START,"preflight")
    for name, targets in {"preflight":["triage_prepare","failure"],
        **{stage+suffix:targets for stage in ("triage","rca") for suffix,targets in (
            ("_prepare",[stage+"_generate","failure"]),("_repair",[stage+"_generate","failure"]),
            ("_generate",[stage+"_validate","failure"]),
            ("_validate",[stage+"_repair","failure","rca_prepare" if stage=="triage" else "completion"]))}}.items():
        graph.add_conditional_edges(name,lambda state:state["next"],{target:target for target in targets})
    graph.add_edge("completion",END); graph.add_edge("failure",END)
    try:
        # No checkpointer, node RetryPolicy, remote graph, tracing, or telemetry callbacks.
        with tracing_context(enabled=False):
            state = await graph.compile().ainvoke({"triage":None,"rca":None,"attempts":[]},
                                                  config={"recursion_limit":32,"callbacks":[]})
        status, reason = state["status"],state.get("terminal_reason")
    except asyncio.CancelledError:
        status, reason = "cancelled","external_task_cancelled"
    except Exception as exc:
        # Persist already completed attempts; do not serialize possibly sensitive exception text.
        status, reason = "processing_failure","unexpected_graph_error:"+type(exc).__name__
    unknown = sum(a.outcome.usage.input_tokens is None or a.outcome.usage.output_tokens is None for a in journal)
    known_in = sum(a.outcome.usage.input_tokens or 0 for a in journal)
    known_out = sum(a.outcome.usage.output_tokens or 0 for a in journal)
    return WorkflowResult(mode=mode,model=adapter.model,status=status,terminal_reason=reason,
        triage=validated["triage"],rca=validated["rca"],attempts=tuple(journal),route_trace=tuple(trace),
        max_generation_attempts=maximum,elapsed_seconds=perf_counter()-started,
        known_input_tokens=known_in,known_output_tokens=known_out,
        total_input_tokens=None if any(a.outcome.usage.input_tokens is None for a in journal) else known_in,
        total_output_tokens=None if any(a.outcome.usage.output_tokens is None for a in journal) else known_out,
        unknown_usage_attempts=unknown,usage_is_mocked=mode=="mocked-transport")
