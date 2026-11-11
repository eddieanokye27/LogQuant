# Offline two-agent contracts and prompt policy v1

This contract layer defines schemas, validation, versioned deterministic requests
and mock-only bounded attempts, independently of provider/LangGraph execution.
No diagnosis accuracy, factual support or prompt-injection resistance is measured.

The subsequent [execution milestone](workflow.md) now connects these unchanged
contracts to LangGraph and an OpenAI adapter, verified with mocked HTTP only.
The contract-only CLI below remains available; real model/schema/token usage
qualification is still pending.

## Response and citation policy

Triage contains cited symptoms, affected services and a chronologically ordered
UTC timeline, plus explicit uncertainty and evidence-limitation lists. Timeline
timestamps must match at least one cited record; a claimed affected service must
occur in its cited evidence. This is structural consistency, not semantic proof.
RCA separates directly cited observations from ranked causal hypotheses. Statuses
are `diagnosis`, `no_incident`, `insufficient_evidence`. Only diagnosis has one or
two hypotheses, ordered ranks 1 then optional 2, distinct exact catalogue names,
probable component, explanation, original citations, and mitigation suggestions
for rank 1. Non-diagnosis statuses have zero hypotheses. Never force rank 2.
Components are application, PostgreSQL or explicitly unknown. Membership in the
retained (run ID, physical line ID) set is required everywhere; omitted, invented
and cross-run references fail. Membership does not establish factual support.

## Prompt boundaries and omissions

Both versioned system templates use the same five-category catalogue and
response JSON schemas. Candidate selection is not proof of an incident or cause.
Original evidence, Triage summaries and repair outputs are untrusted user-role
JSON data, never system instructions. RCA always receives the original evidence
as well as validated Triage. No filtering based on Triage can remove a candidate.

Only the evidence payload plus a checked, allowlisted omission/observed-shortfall
summary enters requests. Full audit reports, source hashes/paths, split/purpose
labels, detector/calibration results, ground truth and injection schedules do not
enter requests. Matching the selection audit to the exact payload prevents
silently claiming omitted evidence was supplied. Diagnostic text remains intact;
any instruction-like text in logs remains data. Boundaries and tests do not prove
an actual model will resist prompt injection.

## Budgets before live integration

Defaults: context capacity 16384 tokens, input cap 12000, Triage output cap 1000,
RCA output cap 1800, RCA Triage-input reserve 2000, timeout 30 seconds per call,
maximum one structured-output repair per agent (configurable 0 or 1). No automatic
transport/SDK retries are permitted in v1. These are implementation defaults,
not proposal/model guarantees. Configuration is validated before constructing
requests. Maximum planned calls per candidate: 2*(1+repair limit), default **4**.
No candidate means zero calls. Unrepaired validation/timeouts are processing
failures, never healthy results or coerced diagnoses.

A counter adapter must count the complete provider-formatted request including
messages, role/framing overhead, response schema and special tokens, and count
output text using the selected model's tokenizer. Its identity and counting unit
are recorded. This provider-neutral layer selects no model; execution v1 supplies
a separately profiled counter. The original offline counter
uses deterministic lexical mock units for tests only; those units are NOT model
tokens and are not derived from UTF-8 byte counts. Live integration must supply
and qualify an actual model counter and context capacity.

Before Triage, count its complete request and an RCA base request with the same
evidence/instructions/schema, reserving 2000 units for Triage. Require input and
context/output space for both stages. Before RCA, validate Triage, check its
serialized input against the reserve, and recount the complete actual RCA request.
Tokenizer counts need not be additive: the actual final request is always checked.
Repair feedback (invalid raw output and validation reasons) is also counted in
full. Never silently truncate evidence, summaries, outputs or repair feedback.
Oversize requests fail explicitly before the corresponding mock attempt.

Mock attempts preserve every consumed raw output, validation reason, request and
its budget accounting. A repair is a separate supplied mock response, not a local
rewrite or category/rank mapping. Accepted repairs remain explicit in the audit;
validation success is not a supported/correct diagnosis claim. Future execution
must enforce timeout/output caps, disable hidden SDK retries, persist failures,
and aggregate billed input/output usage and elapsed wall time across every
initial, failed, timeout and repair call. Mock durations are supplied simulated
values, not measured latency or billable usage; request caps are not usage totals.

## Interfaces and offline CLI

`prepare_evidence(payload, bundle_audit)` verifies matching candidate identity,
retained citations, region counts, payload bytes/hash, UTC bounds and omission
flags. It returns `AgentEvidence`: original evidence plus only the whitelisted
limitations. `validate_response(raw_json, TriageResponse | RCAResponse, evidence)`
rejects malformed JSON, duplicate keys, non-finite numbers, unknown fields,
schema/status/rank errors and citation inconsistencies. It does not strip JSON
fences, sort ranks, map category aliases or invent a fallback diagnosis/status.
Structured citation fields are validated; free-form prose still requires support
review. `unknown` is allowed as a probable component to avoid forced precision.

`preflight` reserves both stages before Triage; `build_request` constructs one
versioned `AgentRequest` with role-separated messages, response schema, output
allowance, timeout and counter identity/count. RCA construction requires Triage
validated against this exact retained evidence. The templates live as `TRIAGE_V1`
and `RCA_V1` constants in `src/logquant/agents/prompts.py`; both use the single
catalogue in `contracts.py`. A provider adapter can consume `request.body`, enforce
the limits and pass raw JSON back to validation. No such adapter exists yet.

The `TokenCounter` protocol has `identity`, `unit`, `count_request(PromptBody)` and
`count_text(str)`. `unit` is `model_tokens` for a future qualified tokenizer or
`mock_units` for offline lexical counting. Counts must be nonnegative integers;
include schema/framing overhead, not only the evidence text. Budget field names
use tokens for the future model contract; all CLI counts are explicitly labelled
mock units and establish no actual model fit/context guarantees.

`run_mock` consumes supplied `MockReply` objects, never a callable/network client.
Each reply has original `output` text and optional simulated elapsed seconds.
It persists every consumed attempt's request, raw output, output count, reasons
and status. Repairs use separately supplied replies and preserve both attempts;
`agents_repaired` makes accepted repairs visible. A valid schema/citation result
has status `validated`, not "correct diagnosis". Triage failure prevents RCA;
RCA failure preserves valid Triage and all failed RCA attempts. Timeout stops the
stage; automatic transport retries remain disabled. Extra unused fixture replies
are never consumed beyond the configured limit.

Install/register the offline command and demonstrate it with the previous
synthetic evidence selection (or reproduce it using [evidence.md](evidence.md)):

```sh
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python scripts/make_mock_agent_responses.py \
  results/evidence/synthetic-v1/bounded results/agents/synthetic-v1/responses.json
.venv/bin/logquant-agents-mock results/evidence/synthetic-v1/bounded \
  --responses results/agents/synthetic-v1/responses.json \
  --output-dir results/agents/synthetic-v1/abstention
```

The generator creates explicitly synthetic canned Triage plus an RCA abstention;
it does not assess cause. Mock input format:

```json
{
  "schema_version": 1,
  "purpose": "synthetic-test-only",
  "candidates": {
    "EXACT_CANDIDATE_ID": {
      "triage": [{"output": "JSON response string", "elapsed_seconds": 0.0}],
      "rca": [{"output": "JSON response string", "elapsed_seconds": 0.0}]
    }
  }
}
```

A second reply per stage supplies an explicit repair. CLI input must be
synthetic-test-only selected evidence; unavailable budget-failure bundles are
rejected. Empty no-candidate evidence uses an empty candidate mapping and consumes
zero mock attempts. `--budget config.json` accepts the `ExecutionBudget` fields;
unknown options/invalid combinations fail. `--overwrite` explicitly replaces
existing mock outputs. Inputs use no-symlink regular-file access, and outputs must
stay outside the evidence directory without overwriting response/config files.
Generators require new output paths; use fresh paths when repeating examples.

Outputs: `mock-results.jsonl` retains all requests, invalid/valid responses and
reasons; `validated-responses.jsonl` contains only fully validated pairs with
`validation_only: true`; `mock-report.json` records configuration, input hashes,
mock counts and processing status. Exit 0 means structurally validated or no
candidates; exit 1 rejects input/config/output; exit **3** means retained
processing failures. Missing/failed output is never interpreted as healthy.
Individual files are atomic; output groups are not crash-atomic transactions.

The original offline-contract milestone added no SDK/tokenizer/graph dependency.
The separate execution milestone now supplies those dependencies. Live
integration must qualify model-specific counting, structured-output schema
compatibility, real cancellation/timeouts and usage reporting. Prompts and schema
tests do not establish semantic correctness, causal support or injection safety.
