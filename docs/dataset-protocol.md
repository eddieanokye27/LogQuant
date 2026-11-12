# Dataset preparation protocol v1 — November 7, 2026

**Prospective amendment, November 11:** The owner-approved
[Mac whole-run protocol](whole-run-protocol.md) separately defines v4 collection
acceptance and schema-2 calibration/evaluation. The original protocol below is
retained; its strict clock and timing-sensitive evaluation claims are not silently
reinterpreted. Original 42-slot assignments and exhausted histories remain.
Research collection still requires qualification and a separately authorized
campaign/realization amendment.

**Readiness update:** Future collection uses the fixed probe/timing/environment
policy in [readiness policy v2](validation/readiness-policy-v2.md). Research slot
counts, categories, splits, seeds, and workload profiles below are unchanged.
V2 invalidates timing-limit violations and dropped arrivals; its stricter rules
supersede the v1 statement that drops are only reported. Retained v1 validation
artifacts are not reinterpreted. Use `plans/validation-readiness.json` for v2
qualification. The v1 plan remains for historical reproduction.

This protocol operationalizes proposal section 5.3 without changing its counts
or five categories. Plans and all collection metadata are evaluation-only.
The plan is an intended set of slots, not a claim that data has been collected.

## Planned research manifest

`logquant-fixture/plans/research.json` enumerates 42 stable neutral slot IDs.
Each slot fixes its whole-run split and complete fixture configuration.

| Split | Healthy | Faulty | Total |
| --- | ---: | ---: | ---: |
| train | 6 | 0 | 6 |
| development | 3 | 10: two per category | 13 |
| test | 3 | 20: four per category | 23 |

Seeds 10001–10042 are unique across slots. Every run schedules 300 seconds of
traffic. The three profiles are baseline/exposure/recovery 50/180/70 at 4 req/s,
60/180/60 at 5 req/s, and 70/180/50 at 6 req/s. Concurrency is four; HTTP timeout
four seconds, verification timeout 15 seconds, and polling sleep 0.25 seconds
remain fixed. Training has two runs of each profile. Development has one healthy
run of each profile and both extreme profiles for every category. Test has one
healthy run of each profile and profiles 4/5/6/5 req/s for every category.
Development and test order is independently shuffled using recorded generator
seeds 20261107 and 20261108; the checked-in order is authoritative. Workload
seeds and profile assignments are fixed in the JSON and need no regeneration.

Traffic generation occupies 3.5 hours in total, plus resets, probes, draining,
collection, and Docker overhead. Baseline duration varies injection timing;
request IDs remain random opaque values. Scheduling is not fully deterministic.
The healthy cases have the same three phases but no injected fault.

The automated plan check rejects wrong counts/categories/splits, faulty training
runs, duplicate seeds or slot IDs, and durations other than 300 seconds. No run
may move between splits. Test data must stay out of tuning and manual diagnostic
review until the analysis configuration is frozen.

## Separate full-duration validation

`logquant-fixture/plans/validation.json` contains exactly six cases: one healthy
and one for each category, all `unassigned`, using distinct seeds 90001–90006.
It exercises the three research workload profiles at full duration. These are
fixture qualification runs, not members of training/development/test, and must
never be promoted into those partitions or counted toward the 42 research slots.

Plan-aware batch execution writes to
`validation-runs/<collection UUID>/<run UUID>/` or
`research-runs/<collection UUID>/<run UUID>/` according to plan purpose. Legacy
smoke/run/batch outputs remain in `runs/`. These output roots are Git-ignored and
mounted separately. Shared raw container evidence is safe only through the
existing half-open per-run export boundaries; check these after collection.

Each collection retains an exact `plan.json`, its canonical SHA-256 in
`manifest.json`, slot-to-run assignments, configuration, status, and invalidity
reasons. Pending entries are written before execution; batch execution stops
after the first invalid/incomplete run. There are no automatic retries. Actual
run IDs remain opaque and exist only after execution, never in the planned JSON.

Commands from `logquant-fixture/`:

```sh
./fixture.sh check-plan plans/research.json
./fixture.sh check-plan plans/validation.json
./fixture.sh start
./fixture.sh batch plans/validation.json
./fixture.sh reset
./fixture.sh stop
```

Only the validation batch is authorized for this milestone. Research collection
is a later task. Neither plan nor collection manifest may be passed to LogQuant;
only explicit diagnostic JSONL files may be used as future model inputs.

## Staged research collection — November 11, 2026

The real-data detector task separately authorizes only the existing six healthy
training and thirteen development slots. The parent remains the complete
`plans/research.json`; do not create a nineteen-row replacement plan or change
its rows. `batch plans/research.json --stage train-development` validates that
parent against the authoritative file and selects only train/development rows
in original order. Test slots s020–s042 remain pending, not executed or inspected.

The collection retains the full `plan.json`, `parent_plan_sha256`, explicit
`stage.name`/`selected_slot_ids`, all attempts, accepted slot-to-run map and
pending parent/stage slots. Its final state is **stage_complete**, with
`stage.status=complete`, no pending selected slots, and
`parent_plan_complete=false`. This is not completion of all 42 slots. An invalid
or interrupted attempt leaves **stopped**, with retained reasons and pending
slots. Existing full validation batches retain their original `complete` contract.

Training/calibration recognize a selected stage only after verifying its parent
snapshot/hash against the unchanged authoritative plan, exact configs/splits,
sequential bounded attempts, unique accepted IDs, all nineteen selected accepted
slots and the twenty-three pending test slots. They continue rejecting stopped,
incomplete, conflicting, readiness-validation or wrong-split inputs. A stage is
evaluation metadata; it never enters diagnostic ingestion or vectorizer text.

There is no automatic resume/retry. The regeneration rules below still govern
manual replacements: retain the original invalid attempt, report/remedy its
collection failure, record pending replacement intent and outcome in the
evaluation-only ledger, reuse the exact planned config and accept the first
valid attempt within three total. Do not recreate valid slots or replace runs
based on detector/LLM results. A selected stage cannot be made eligible merely by
editing its status; the complete assignment/provenance checks must pass.

After explicitly authorized collection, run from `logquant-fixture/` using the
analysis environment:

```sh
../.venv/bin/python scripts/verify_validation.py \
  research-runs/<collection-UUID> --research-stage
```

This reuses the full-duration timing, independent fault/recovery, native evidence,
count/export-boundary and bounded leakage checks, with explicit selected-stage
eligibility. It writes a separate `stage_report.json`; validation-only mode and
its old reports remain intact. Collection changes revise image/source identities
and require six-case full-duration qualification before research collection.

## Acceptance and regeneration rules

1. Apply validity checks without looking at detector scores, LLM outputs, or
   diagnostic accuracy. A valid run requires healthy baseline and recovery
   operations; healthy runs additionally require successful exposure traffic.
   Faulty runs require independent state plus unsuccessful application probes,
   maintained verification during exposure, expected ordinary application error
   evidence, and native lock-wait evidence for lock cases. Successful recovery,
   cleanup, parseable bounded exports, and the leakage audit are required.
2. Missing/partial metadata, interruption, failed setup/injection/verification,
   lost verification, failed recovery/cleanup, and missing/corrupt/contaminated
   diagnostic exports are invalid collection attempts. Retain every directory
   and reason. Do not reconstruct missing observations or label an incomplete
   run valid. For a hard crash, add an evaluation-only incident record with the
   incomplete slot/run mapping, observed facts, and missing artifacts.
3. Stop collection on any invalid attempt. Investigate the recorded failure,
   reset, and confirm readiness before retrying. A slot allows **at most three
   attempts total** (initial plus two replacements). Retry manually with the
   identical category, split, seed, durations, rate, concurrency, and timeouts,
   using a new opaque run ID; never overwrite or recycle an attempt directory.
   No automatic resume/retry command is implemented in this milestone.
4. Before a manual retry, create/update an evaluation-only `regeneration.json`
   in the original collection. Record the plan hash, slot ID, sequential attempt
   number, predecessor run ID (or incomplete path), invalidity reason, remedial
   action, timestamp, exact config, replacement path/run ID, and outcome. A
   replacement may be executed through the existing `experiment(cfg, root=...)`
   API using the original collection root. Record pending intent before starting
   and completion afterwards. The operator must enforce the three-attempt limit;
   the initial batch runner does not enforce manual retry accounting.
5. Accept the **first valid attempt**, never the most diagnostically convenient
   one. The retained assignment ledger must map each research slot to at most
   one accepted run and link all excluded attempts. Do not retry a valid run
   because it has few errors, unfamiliar messages, poor detection/diagnosis,
   abstentions, or expensive LLM processing. Drops/latency are reported workload
   measurements, not grounds to replace an otherwise valid run.
6. If all three attempts fail, leave the slot unresolved and stop. Report the
   incomplete dataset; do not silently reduce denominators, substitute a different
   split/category/seed, or manufacture a valid example. A fixture-code, logging,
   dependency, or protocol change requires a documented version/fingerprint
   change and requalification of the six validation cases before resuming.
   Do not silently mix collection versions into one reported dataset.

## Full-duration evidence review

After the six cases, verify actual elapsed phase coverage from evaluation
request timestamps, fault observation/recovery boundaries, nonoverlapping
export intervals, diagnostic record counts, and healthy/recovery outcomes.
Review representative ordinary records for each category; distinguish auth
failure from permission failure and native lock waits from generic timeouts.
Check all diagnostic JSONL for labels, control SQL, metadata, known credential
values and run/slot IDs. Retain the audit report outside diagnostic directories.
Report observed probe gaps, dropped arrivals, and load limitations. This review
does not create the future LogQuant parser or establish diagnostic performance.
