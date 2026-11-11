# Offline evidence bundle contract v1

Selection consumes strict original ingestion plus frozen alert output for one
explicit run. It neither loads a detector/calibration artifact nor reads evaluation
metadata. Existing threshold comparison, edge handling and candidate grouping
remain unchanged; the shared classifier reconstructs and verifies supplied groups.

## Boundaries and selection policy

For candidate [start,end), request [start-before,end+after). Context defaults to
60 seconds on each side; integer configuration permits 0..60 only. Before is
[requested_start,start), candidate is [start,end), after is [end,requested_end).
All timestamps are UTC. Following context makes this **offline evidence**, not
real-time incident response. Requested bounds are not collection boundaries.
Sort final records by timestamp, application before PostgreSQL, physical line.
Preserve legitimate equal/duplicate events under their distinct physical IDs.

Validate all window identities, intervals, states, candidate membership/order,
first-alert times and original line references against complete supplied records.
Reconstruct groups with the existing shared classifier; never silently repair an
altered group. Match diagnostic fingerprints before selecting. Run identity stays
alongside physical line IDs; no lookup crosses runs. Caller identity/provenance
must be trusted: identical or relabelled files cannot establish run authenticity.

Default per-candidate budgets: **200 records** and **262144 serialized UTF-8
bytes**. Byte limits apply to the exact final agent payload, including metadata,
JSON escaping, raw records/text and its terminating newline, not audit files or
the output list across candidates. Bytes are not tokens. Messages are never cut.

Try all candidate-region records in chronological order first, skipping whole
records that cannot fit. Then alternate nearest-before, nearest-after, starting
before; each side orders by distance to its boundary, then source/physical line.
Skip oversized records and continue looking for smaller ones. Serialize every
trial in final chronological order to measure the actual payload bytes. Both
context sides are retained when remaining record/byte budgets permit; no context
reservation overrides candidate priority. This simple policy has chronological
bias and makes no claim to identify the most causally useful record.

A valid payload must retain at least one candidate-period record. If even the
smallest such payload cannot fit, return an explicit per-candidate budget failure
with no agent payload, all observed requested records omitted and a retained audit
report. Invalid configuration fails explicitly. No-candidate input produces an
empty payload list and a `no_candidates` audit result, never an incident.

## Preservation, omissions and separation

Payloads contain only candidate/requested intervals, offline designation and
original `LogRecord` objects with run-qualified citations and region markers.
Raw record, raw text, original message/exception wording, optional SQLSTATE and
source identity are unchanged. Feature-normalized text is not used.

The separate audit retains configuration/policy version, candidate identity,
source and alert-input fingerprints, payload bytes/hash, retained citations,
available/retained/omitted counts by region, oversized-individual counts, and
whether candidate evidence was omitted. "Oversized individual" means the complete
single-record payload exceeds the byte budget even without any other records.
Observed requested records omitted for budget reasons are distinct from absent
observations. Report the first/last observed event and each context edge's distance
to those extrema (capped at requested context duration). These **observed-extent
shortfalls** and zero available counts do not prove lost logs or continuous
coverage. Never infer collection start/end or extend context to replace silence.

## API and CLI

`select_evidence(ingestion, run_id=..., alerts=..., diagnostic_sha256=...,
config=..., alert_input_sha256=...)` accepts a complete `IngestionResult`, an
`AlertResult` and its diagnostic-file hashes. It returns payloads plus a separate
report. Incomplete inputs, mismatched fingerprints, altered/missing groups/windows,
wrong run IDs and inconsistent physical citations fail before selection.
Input order is canonicalized. This verifies grouping and record consistency;
it does not authenticate upstream detector scores or prove run provenance.

CLI input is exactly one diagnostic directory plus an alert directory containing
`alerts.jsonl`, `candidates.jsonl`, `alert-report.json` from frozen classification.
Only the existing literal two-file diagnostic allowlist and these three alert
files are read. No recursive loading or calibration/ground-truth read occurs.
Symlinks and nonregular files are rejected using the existing POSIX boundary.
The alert report supplies source fingerprints; its unrelated fields are not
copied into payloads or reports. The audit keeps only alert-file hashes.

Optional config JSON (all defaults shown):

```json
{
  "before_seconds": 60,
  "after_seconds": 60,
  "max_records": 200,
  "max_payload_bytes": 262144
}
```

Unknown options, booleans/fractional integers, negative values, zero budgets and
context above 60 seconds fail explicitly. Install the command without resolving
new dependencies and generate the synthetic demonstration once:

```sh
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python scripts/make_synthetic_evidence_example.py results/evidence/synthetic-v1
.venv/bin/logquant-evidence results/evidence/synthetic-v1/diagnostic \
  --run-id synthetic-evidence-01 --alerts-dir results/evidence/synthetic-v1/alerts \
  --output-dir results/evidence/synthetic-v1/full
.venv/bin/logquant-evidence results/evidence/synthetic-v1/diagnostic \
  --run-id synthetic-evidence-01 --alerts-dir results/evidence/synthetic-v1/alerts \
  --config results/evidence/synthetic-v1/tight-config.json \
  --output-dir results/evidence/synthetic-v1/bounded
```

The synthetic generator uses **manually assigned scores**, grouped by the shared
classifier, and is explicitly marked test-only. No detector discovered these
incidents. It creates `no-candidate-alerts/`, `two-candidate-alerts/` and an
`impossible-config.json` (one-byte limit) for other CLI paths. It never fits a
model, calibrates, injects a fault or reads research/readiness cases. Choose a
new generator directory when rerunning; existing inputs are never overwritten.

The CLI writes `evidence.jsonl` (one exact agent-facing JSON payload per candidate
that fits) and separate `evidence-report.json`. Each payload's **whole physical
JSONL line including newline** is the budget unit. The combined run file, audit,
and future prompt instructions have separate sizes; this is not an API context
or token guarantee. Payload and audit link through candidate ID and payload hash.
No candidates produces empty `evidence.jsonl`; budget failures also omit invalid
payloads but retain one explicit audit entry per candidate. Some candidates can
succeed while others fail. Future consumers must inspect the report rather than
interpret missing payloads as healthy/no candidate.

Exit codes: 0 selection or explicit no-candidate result; 1 rejected contract,
boundary or existing output; 2 diagnostic ingestion failure; **3 budget failure
with audits retained**. `--overwrite` explicitly replaces the two output files;
output directories must stay outside diagnostic/alert sources. Original logs
remain untouched. Files are individually atomic, not a multi-file transaction.

These are in-memory deterministic selection rules for bounded local experiments.
Candidate-period chronological priority can omit later/large diagnostic errors;
oversized records may lose essential context. The report exposes omissions but
does not repair them or establish causal sufficiency. Overlapping candidates can
reuse evidence; their independent bundles are not deduplicated across incidents.
Raw diagnostics must be treated as untrusted data by future agent consumers.
