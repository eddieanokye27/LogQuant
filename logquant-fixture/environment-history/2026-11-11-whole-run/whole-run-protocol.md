# Mac whole-run feasibility protocol v1

The project owner explicitly approved the
[Mac-only amendment](validation/2026-11-11-mac-clock-amendment.md) for
implementation, synthetic tests and one new nonresearch qualification suite.
This is a **prospective evaluation amendment**. It is not supervisor approval.
The original [proposal](proposal.md), dataset protocol, old policy artifacts,
run labels and exhausted counters remain preserved. Research collection and
research fitting/calibration require a separate authorization after qualification.

## Effectiveness and candidate selection

The primary effectiveness outcomes are **end-to-end top-1 and top-2 failure-category
diagnosis across all faulty runs** (the planned final-test denominator remains 20).
A missed candidate, abstention, no-incident/insufficient-evidence response,
unsupported/unrepaired output or execution failure is unsuccessful at both cutoffs.
A single correct hypothesis earns both top-1/top-2; a correct second hypothesis
earns top-2 only. Keep the exact five proposal categories and existing typed
rank/citation/response validation.

For filtered workflows, select the **first candidate in recorded-UTC order**,
using `(candidate.start, candidate_id)`, before looking at labels, verification
intervals or diagnosis correctness. Never substitute a later correct result.
The same choice applies to future evidence-only comparison. Full-log baseline
predictions remain run-level predictions. This task does not implement those
comparison variants or final scoring pipelines.

Supporting outcomes:

- Whole-run candidate recall: faulty runs with any candidate / all faulty runs.
  An alert before injection or during recovery can count; **this does not establish
  detection during the fault**.
- Healthy-run alert fraction, raw candidate counts and score/null/edge coverage.
  All-unscorable cases are separate unknowns, not demonstrated healthy behavior;
  retain denominators and report uncertainty rather than deleting them.
- Conditional RCA category accuracy among faulty runs with a validated selected
  **diagnosis** response: report that exact denominator alongside all-run primary
  results. Also report candidate-bearing runs, abstentions and failures separately.
  Undefined/zero-denominator ratios are null, not invented zeros or successes.
- Timing flags stay in evaluation/summary results. They do not enter detector
  documents or agent requests. No-candidate output alone does not prove health.

The original first-alert-during-verified-failure RCA selection and physical-time
incident-recall point claims are amended explicitly. Optional timing recall may
report definite/ambiguous/miss counts and bounds over every faulty run, only from
justified uncertainty sets. There is no fabricated global ±1.086-second bound.
Unresolved timing-dependent RCA, physical-hour false-alert rates, exact onset and
physical time-to-detect are omitted. Recorded-UTC comparisons are nominal only.

Workflow processing latency uses the existing elapsed `perf_counter()` intervals:
include all provider attempts, validation/repair and failures in measured processing
time/usage reports. That is processing the saved evidence, **not time-to-detect
from fault onset**. Following evidence context makes the workflow offline. Unknown
provider usage remains unknown; local cancellation does not prove billing stopped.

## Collection policy `acceptance-v4-whole-run`

`valid` remains a compatibility alias for `collection_valid`. Both require complete
workload/verification/recovery, cleanup and uncontaminated diagnostic evidence.
`timing_status=unassessable` explicitly states that continuous cross-service clock
alignment is not certified, even if all sampled readings appear consistent.
`clock_discontinuity_observed`, bracket widths, clock warnings and signed offsets
provide the actual observed clock evidence. A clock-only one-second crossing is
retained as a warning; it is not a successful timing assessment or a reason to
accept incomplete data. All fatal timing violations have their own retained list,
so capped clock warnings cannot hide a later heartbeat/queue failure.

Each clock sample records `monotonic_before → time.time → monotonic_after` and
offset interval `[wall-mono_after,wall-mono_before]`. Initial reference, phase
endpoints, maximum/last sample and a bounded 20-sample first-change context are
retained. These bound those observations only. No interpolation or retiming is
performed; PostgreSQL clock alignment remains unknown.

The absolute seeded scheduler, half-period spacing and skip policy remain.
The same numeric 5% nominal-bucket/1% run offered-load budgets now use **run-relative
monotonic** 30-second bins anchored at the initial monotonic-after sample. Skipped
or displaced arrivals count once in the run union and in each affected bin;
equality passes with the existing integer floors. UTC bucket distortion is not
bounded by these offered-load counters. Old v3 audits retain their UTC basis.

Phase duration and target-plus-six-second overrun checks use monotonic time.
Keep the one-second heartbeat/queue limits, half-second probe launch limit,
HTTP/control deadlines, all required probe cycles, successful healthy operations,
independent maintained faults, recovery and queue-full rejection. Monotonic fault/
recovery observations preserve ordering separately from their original UTC values.
Reversed run UTC endpoints still reject collection; wall elapsed times are retained
but are not interpreted as physical observation coverage.

## Physical export and reconciliation

After setup/readiness settles, snapshot application and native PG collector
file paths, device/inode, byte size and prefix hash. After recovery/draining and
settling, snapshot ends **before cleanup**. The measured monotonic observation
envelope covers both snapshots and control/draining.

Export only those complete physical byte ranges, retaining original UTF-8 bytes,
timestamps and duplicate events. Old files must retain identity/prefix; new native
restart files begin at zero and are included. A restart appending to an existing
file retains its identity and nonzero start offset. Missing/replaced/truncated
files, changed prefix bytes, partial JSON lines, symlinks, unsettled sources or
unexpected writes at export invalidate collection. Each destination is created
exclusively. Per-file output partitions, range hashes, before/after state and
counts remain evaluation-only. Independent audit verifies copied partitions and
nonoverlap of physical ranges across the new collection; UTC is not used to
filter the slices a second time. This is not a cryptographic attestation of truth.

Application counts must equal tracked workload plus probe requests. Every returned
`X-Request-ID` must match exactly one unchanged application record with matching
method/route/status; missing IDs are explicit unknowns, not fabricated links.
Required probe/fault failures still reject the case. PostgreSQL may be empty.
File-size stability remains a bounded flushing heuristic, not a formal collector
acknowledgement protocol; independent counts and native-evidence checks remain.

Before ML results, require at least two nonempty interior diagnostic buckets and
`N <= ceil(M/30)+2`, where `N` is observed diagnostic grid size and `M` the monotonic
observation duration. This is a representation/feasibility budget, not a bound
on every event's error or proof of unchanged detector features. All quality gates
are independent of detector/LLM results. The diagnostic loader, normalization,
epoch windows, empty/unknown-edge handling and evidence selection are unchanged.
Their intervals describe recorded UTC ranges, not certified physical durations.

## Development calibration `whole-run-v2`

The original `verified-interval-v1` selection, artifact schema and CLI default
remain. New artifacts use schema 2 and an explicit whole-run policy identifier;
wrong-policy loading fails. Frozen inference reuses the same model and classifier
without refitting or reading ground truth. `AlertResult.policy` remains the shared
legacy classifier/grouping identifier for evidence-contract compatibility;
`threshold_calibration_policy` in the CLI report and schema-2 threshold artifact
identify the **selection** policy. The unchanged evidence loader verifies the same
window/record identities; no threshold labels/timing flags enter evidence payloads.

Use one fixed detector and only eligible complete development runs. Reject training,
test, readiness, incomplete/duplicate/conflicting data as before. Research inputs
under the new policy must have amended complete verification; explicitly marked
synthetic-only fixtures are permitted with synthetic-only detectors. Labels and
timing flags are read only in orchestration, outside feature/vectorizer text.

Require whole-run faulty candidate recall **>=0.8** and **zero healthy candidates**.
Each healthy development run must have at least one finite interior score; all
faulty runs, including null/edge-only misses, remain in the recall denominator.
Search all distinct usable scores plus the finite float immediately below the
minimum; use strict `score > threshold`. Grouping/null/edge behavior is unchanged.
Among feasible thresholds maximize recall, minimize healthy count, then choose
the highest threshold. Retain all infeasible trials, reasons and null threshold;
never relax settings because of outcomes. These settings are owner-approved
prospective configuration, not a proposal requirement or population guarantee.
No physical-hour denominator is used; nominal score-grid seconds remain coverage
descriptions. Persist timing flags, provenance, input hashes and model fingerprint.

```sh
.venv/bin/logquant-threshold calibrate PATH/development-selection.json \
  --policy whole-run-v2 --config configs/whole-run-calibration-v2.json \
  --detector-dir PATH/trusted-detector --output-dir PATH/whole-run-threshold
.venv/bin/logquant-threshold apply PATH/diagnostic --run-id RUN_ID \
  --policy whole-run-v2 --detector-dir PATH/trusted-detector \
  --threshold-dir PATH/whole-run-threshold --output-dir PATH/alerts
```

Outputs refuse accidental overwrite; `--overwrite` is explicit. Trusted project
pickles only. Unsuccessful calibration cannot classify. This task uses synthetic
data only; these commands do not authorize research fitting/calibration.

## Qualification allowance and lineage

The owner separately authorized **one new** six-case full-duration nonresearch
suite with the original readiness plan, order, seeds and profiles, AC/awake/open
lid and existing sleep guard. Stop first collection-invalid/incomplete case;
zero replacements/repeated suites/extra profiling or enlarged limits. Clock-only
uncertainty stays visible under the amended rule. All six plus independent
physical-export, native fault/recovery and bounded leakage audits are required
for whole-run collection qualification, not true-time qualification.

The CLI takes `--qualification-record plans/qualification-whole-run-v1.json`.
It validates the parent/policy/predecessor, claims an exclusive one-shot record,
and snapshots predecessor manifests/hashes and cumulative attempt counts for
identical parent-plan cases. Each new local attempt is 1 with an explicit
cumulative number. The stopped predecessor remains exhausted and byte-identical.
No new UUID resets old research s004's three attempts. Research campaign/version/
old-data/additional-realization decisions and actual collection remain separate.
