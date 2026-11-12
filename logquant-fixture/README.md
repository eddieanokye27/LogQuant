# LogQuant local evaluation fixture

A small inventory/order API, PostgreSQL 17, and an experiment harness. This is only
the data-generation fixture: no detector, agents, dashboard, or diagnosis metrics.
The five category strings exactly match [`docs/proposal.md`](../docs/proposal.md).

## Quick start

Requires Docker Engine/Desktop with Linux containers and Docker Compose v2 with
`up --wait` support. The harness uses the Docker Unix socket to stop/start **only**
the Compose service labelled `logquant-fixture/db`. The Docker socket grants broad
host control to the harness container; use this reviewed local fixture only on a
trusted development machine. Do not deploy it as a public service. Rootless or
nonstandard Docker socket locations require changing the harness socket mount.

```sh
cd logquant-fixture
cp .env.example .env
chmod +x fixture.sh
docker compose pull db
./fixture.sh build
./fixture.sh start
./fixture.sh test
./fixture.sh run --smoke
./fixture.sh run --smoke --category 'Database lock contention'
./fixture.sh integration
```

`build` installs all resolved distributions from `requirements.lock` on a
digest-pinned Python base and records immutable local image references in
`compose.lock.yaml` and `environment.lock.json`. `start` uses those frozen
images with no builds or pulls, starts the two services and initializes the schema,
restricted role, and deterministic seed data. API: http://localhost:8000/docs.
PostgreSQL has no published port. A different API port can be set in `.env`.
Do not send other traffic during collection: external traffic is not tracked by
the harness and can contaminate the experiment.

The integration command runs one healthy case, then each of the five faults
followed by a separate healthy case. It checks validity, ordinary diagnostic
messages, recovery, and rejection of a deliberately no-op injection. Expect
several minutes. Stop and inspect the evaluation metadata if any case fails.
Smoke runs (3/5/3 seconds plus probes/reset/draining) verify plumbing only; they
are not research data and establish neither detector performance nor data quality.

## Full experiments and batches

```sh
./fixture.sh run
./fixture.sh run --category 'Database unavailable' --seed 200 --rate 6
./fixture.sh run --category 'Invalid database credentials'
./fixture.sh run --category 'Application exception'
./fixture.sh run --category 'Database permission denied'
./fixture.sh run --category 'Database lock contention'
./fixture.sh run --baseline 60 --exposure 180 --recovery 60 --rate 5 --concurrency 4 --seed 42
./fixture.sh batch batch.example.json
./fixture.sh reset
./fixture.sh stop
```

Edit `batch.example.json` to add any number of runs. It is explicitly mounted into
the harness; for another batch file add an equivalent read-only mount in Compose.
Each entry has `config` fields matching the run CLI and an evaluation-only `split`
of `train`, `development`, `test`, or `unassigned`. Faulty training runs are rejected.
The sample is a seven-run configuration example, **not** the proposal's complete
dataset. Entire opaque runs are assigned to a split; windows are never split here.
Batch execution stops on an invalid run and saves progress to a unique
`runs/manifest-<opaque-id>.json`. Prior manifests remain intact. Regeneration is
manual: retain invalid runs and their reasons instead of silently replacing them.

### Complete dataset plans

[`plans/research.json`](plans/research.json) is the complete **planned** 42-slot
research manifest (6 train, 13 development, 23 test).
[`plans/validation.json`](plans/validation.json) is a separate six-case,
full-duration fixture qualification batch, with no research split assignment.
The versioned acceptance/regeneration rules are in
[`docs/dataset-protocol.md`](../docs/dataset-protocol.md).
The current probe, timing, sleep-guard, and environment policy is
[`readiness policy v2`](../docs/validation/readiness-policy-v2.md).
Use `plans/validation-readiness.json` for the updated six-case qualification;
the original `plans/validation.json` is retained with its old collection records.

```sh
./fixture.sh check-plan plans/research.json
./fixture.sh check-plan plans/validation-readiness.json
./fixture.sh batch plans/validation-readiness.json
```

`check-plan` validates without collecting data. Plan-aware batches record an
exact plan snapshot, its SHA-256, and a slot-to-run collection manifest under
`validation-runs/<collection UUID>/` or `research-runs/<collection UUID>/`.
These roots are mounted separately and Git-ignored. The six validation runs
must never enter research splits. The batch stops on the first invalid case and
does not automatically retry. Manual regeneration permits at most three total
attempts per slot and requires an evaluation-only ledger; see the protocol.
Do not execute the research plan as part of fixture validation.

For a separately authorized research train/development task, keep the full plan
and select the stage explicitly:

```sh
./fixture.sh batch plans/research.json --stage train-development
../.venv/bin/python scripts/verify_validation.py research-runs/<collection-UUID> --research-stage
```

Only s001–s019 execute. The retained full parent-plan hash, stage selection,
attempts, accepted run IDs and pending slots distinguish `stage_complete` from
completion of all 42 slots. Test slots remain pending. Stopped/interrupted stages
are ineligible for fitting/calibration; existing regeneration rules still apply,
with no automatic retry/resume. Changes to the baked harness source require an
explicitly revised image lock and six-case requalification before research use.

Current research collection stopped after s004 exhausted three timing-invalid
attempts; only s001–s003 are accepted. See the
[actual results/blocker](../docs/validation/2026-11-11-real-data-detector.md).
`batch` starts a new collection, not a resume: do not use it to reset exhausted
slot counters or recollect accepted slots. Further collection needs an explicit
protocol/campaign decision after resolving scheduling continuity.

The subsequent [continuity investigation](../docs/validation/2026-11-11-continuity-investigation.md)
clarified producer lateness versus queue/HTTP latency and added evaluation-only
[trace v3 fields](../docs/validation/continuity-trace-v3.md), without raising limits.
The current locked trace image failed qualification on its first healthy case;
it is **not research-qualified**. The cause remains unresolved. Do not restart
research from the example command or treat diagnostic repetitions as replacements.

After a complete validation batch, review its exports with the standard-library
helper (from this directory):

```sh
python3 scripts/verify_validation.py validation-runs/<collection-UUID>
```

It writes `validation_report.json` beside the collection manifest, verifies
observed phase coverage and export boundaries, and checks native error states
and selected leakage markers. Review representative messages as well; this
helper is fixture qualification, not the future LogQuant ingestion parser.

Defaults: baseline 60 s, exposure 180 s, recovery 60 s, 5 scheduled requests/s,
4 traffic workers, total HTTP timeout 4 s, fixed HTTP probe suite every 10 s,
within-suite independent-state polling sleep 0.25 s,
verification timeout 15 s. CLI flags also include `--timeout`, `--poll`, and
`--verify-timeout`. Durations are target traffic-generation durations; draining
pending requests, injection, checks, startup, and cleanup add wall-clock time.
HTTPX timeout bounds network phases; PostgreSQL connections use a 2 s timeout,
statements 1.5 s, and lock waits 0.8 s. Administrative statements are bounded at
2.5 s, DB startup retries at 30 s, Docker HTTP operations at 30 s. No experiment
command intentionally waits indefinitely.

A bounded request queue records dropped arrivals when the target rate exceeds
capacity. Requests are never automatically retried (particularly writes).
Initial readiness retries are bounded. Failed injection/recovery is not retried
as a successful experiment; it invalidates the run. Seeds control the ordinary
request sequence, not OS scheduling, request completion order, or measured latency.
Use different recorded seeds/rates and, if desired, baseline durations in batch
configs for controlled variation. Request IDs are fresh random UUIDs without
encoded seed/category information. Orders use deterministic product IDs 1–10.

## Failure catalogue

| Exact category | Controlled condition and exposing operation | Independent proof | Expected ordinary evidence | Recovery |
|---|---|---|---|---|
| Database unavailable | Stop DB; readiness/read/write requests connect | Docker database state is `exited` plus HTTP 500 | Connection refusal/resolution/connection errors; database shutdown logs | Start DB; wait for SQL availability |
| Invalid database credentials | Change database role password while app keeps original; all DB operations | Separate connection with original password receives authentication rejection plus HTTP 500 | PostgreSQL authentication failure | Restore original role password |
| Application exception | Set product 1 packing quantity to zero; ordinary quote request | Query stored value plus HTTP 500 | Real `ZeroDivisionError` | Restore packing quantity to six |
| Database permission denied | Revoke INSERT on orders; ordinary order | Fresh HTTP reads succeed, admin privilege observation is false, order returns HTTP 500 | Permission denied for orders / SQLSTATE 42501 | Grant INSERT |
| Database lock contention | Keep exclusive orders lock in open transaction; ordinary order | `pg_blocking_pids` identifies holder blocking a `shop` connection plus HTTP 500 | Native lock wait and lock-timeout errors | Close lock session |

All are real induced conditions, not fabricated error log strings. The application
exception deliberately uses invalid data to expose a missing zero-denominator
guard; it is a narrow controlled software-failure case, not an arbitrary crash or
a production bug-discovery benchmark. The corrupted value is not reachable through
an injection endpoint. No category-named endpoint, request parameter, environment
variable, or application feature flag exists. App uses fresh non-superuser
connections and has SELECT on products, SELECT/INSERT on orders and sequence use.
Only the harness has administrative credentials.

The normal traffic mixes product reads, product quotes, and orders. Verification
probes launch the same four operations every ten seconds in every phase and case,
using absolute launch deadlines. They use those operations plus readiness; probes are ordinary requests and
are included in diagnostic logs. Their outcomes are recorded separately in
`observations`, not in workload throughput summaries. This extra probe load and
its temporal regularity are experimental limitations to report. Full-duration
cases add exactly 120 HTTP probes (6.67–10% above scheduled traffic); outcomes and
completion timing vary with the real fault. Admin observations stay in evaluation.
Permission checks use an admin privilege query plus successful fresh HTTP reads,
rather than extra deliberately failing SQL INSERTs. Independent auth checks still
produce genuine native authentication errors. See the v2 policy for detail.

## Lifecycle and output

One cross-process file lock prevents concurrent experiments or resets within this
fixture. A run resets state, checks readiness, collects baseline traffic, injects
one fault, checks its effects repeatedly alongside exposure traffic, removes it,
checks all operations, and collects recovery traffic. A final reset occurs on
success, exceptions, first Ctrl+C, and SIGTERM. Fault sessions are also closed by
connection teardown. `reset` terminates stale control sessions from a killed
harness. SIGKILL, host crashes, repeated interruption, or Docker loss cannot
promise cleanup; run `./fixture.sh reset` before resuming. Such incomplete runs
must not be used as valid examples.

Validity also requires successful healthy workload requests and exported ordinary
application evidence for the chosen fault. Lock cases additionally require native
PostgreSQL lock-wait diagnostics, alongside independent blocking verification.
Database-stop evidence accepts connection and DNS resolution failures, since a
stopped Compose service can lose its DNS record. Missing evidence invalidates the
run even when injection and recovery probes pass.

Every run creates:

```text
runs/<opaque UUID>/diagnostic/application.jsonl
runs/<opaque UUID>/diagnostic/postgresql.jsonl
runs/<opaque UUID>/evaluation/ground_truth.json
runs/<opaque UUID>/evaluation/requests.json
runs/<opaque UUID>/evaluation/summary.json
```

Give LogQuant **only the diagnostic directory**. Application and DB files remain
separate. Application records include UTC timestamp, service, severity, event,
request ID, method, concrete route, status, duration, and exception type/message
when an operation raises. API clients receive a generic error. Credentials,
headers, request bodies and injected labels are not logged. Numeric product IDs
in ordinary routes are workload data, not experiment metadata.

PostgreSQL records retain native JSON fields and messages. Statement logging,
error-statement logging below PANIC, connection logging, and disconnection
logging are disabled. No audit extension is enabled. Administrative control SQL
and its password literals are not exported as statements. Genuine auth,
permission and lock errors remain visible. The harness has no logger writing
into diagnostic files. The application has no access to evaluation metadata.

`ground_truth.json` includes designation/category/component, validity/reasons,
config/seed, actual start/end boundaries, injection attempt/completion,
first/last observed verified failure, recovery attempt/verification, state and
request observations with sampling timestamps, traffic summary, source/config
fingerprint, and log counts. An observed interval means **sampled confirmation**,
not proof that every instant between samples failed. Injection timestamps are
not substituted for observed failure timestamps. Polling resolution is a target
sleep: actual sample gaps include probe latency and SQL/Docker checks. Those
actual timestamps are retained. `simulation=false` denotes actual induced
conditions; the application-data corruption limitation is separately recorded.

`requests.json` records workload request completion timestamps, statuses,
latencies and phase names, exclusively for evaluation. Latency/count summaries
exclude verification requests. Invalid/incomplete runs return nonzero exit status.
No detector metrics are computed.

## Evidence collection and limitations

Application JSONL and PostgreSQL JSON files live in a shared persistent volume.
The database collector survives across restarts through these files, so evidence
before a stop is retained. Export reads all native files and uses a half-open
UTC interval `[start, end)`, after setup readiness and before final reset.
Application timestamps are request completion times, not request arrival times.
PostgreSQL timestamps are native event times. All containers share the host clock.

Traffic is drained; an additional three-second server drain precedes the end
boundary. Export then waits for three unchanged file-size samples, bounded at
five seconds. This is a practical local-collector flush heuristic, not a formal
acknowledgement protocol: validate it on the target machine under load. Malformed
JSON or unsettled logs invalidates the run. The collector scans historic files;
this is simple for a short undergraduate dataset but becomes slower as the
volume grows. Historical raw evidence is intentionally not deleted between runs.
Export overwrites a new run's files once, preserves duplicate *real* messages,
and never appends a repeated export. Empty DB output is permitted and explained;
successful SQL is not logged. Missing application evidence is invalid.

The automatic leakage audit checks category labels, control SQL markers and
metadata keys. It is a guardrail, not proof against all leakage. Manually review
representative exports for administrative statement text, credentials, revealing
paths/identifiers and probe regularity before freezing the experiment. Normal
errors may reveal the real cause, which is intended. Lock diagnostics can include
process IDs and ordinary blocked SQL; these are legitimate database evidence.

All resolved Python versions (including pip) are pinned in `requirements.lock`;
the Python base and PostgreSQL use immutable image digests. Locally built
application/harness image identities, platform, tool versions, and lock hash are
retained in `environment.lock.json` and copied into each collection/run. The
source fingerprint also covers the dependency lock. Exact local image references
must remain in Docker's image store; archive with `docker image save` if moving
machines. Independent rebuilds may produce new custom image digests and require
qualification. This targets Linux/arm64 Docker Desktop on the development Mac.
There is no claim that five narrow cases generalize to production incidents.

Collection commands automatically use `caffeinate -i -s` on macOS and record the
guard in evaluation metadata. Elsewhere they warn and record it as unavailable.
Do not close the lid or force sleep. Evaluation-only request dispatch/queue
metrics, a 100 ms dual-clock heartbeat, and phase timing enforce fixed acceptance
limits. Original v2 uses a 0.5 s workload/probe scheduler cutoff and rejects any
dropped arrival. Prospectively, [policy v3](../docs/validation/readiness-policy-v3.md)
skips expired workload slots without replay, preserves the seeded slot mapping,
and caps skip/bucket-migration distortion at 5% nominal bucket volume and 1% per
run. Probe scheduling remains 0.5 s, queue/heartbeat/clock remain 1 s and phase
overrun remains 6 s; queue-full drops remain invalid. Slow awaited fault responses
are measured separately. Exact accounting is retained in evaluation ground truth
and replayed by the export audit; original v2 results retain their old rules.
The revised environment is frozen/archived but is not research-qualified. Its
single nonresearch suite passed healthy/database-stop cases, then stopped on
clock divergence in the credential case; three cases were unattempted. See the
[execution report](../docs/validation/2026-11-11-v3-qualification-execution.md).
That allowance is consumed; no replacement or repeat is authorized. Research
collection remains separately blocked.

The owner subsequently approved the separate [Mac whole-run amendment](../docs/whole-run-protocol.md)
and **one new** qualification suite. Implemented `acceptance-v4-whole-run` exports
complete physical collector ranges, checks identities/prefixes/reconciliation and
cross-case nonoverlap, uses monotonic phase/load coverage, and retains bracketed
clock readings. Collection validity is separate from unassessable physical-time
alignment; a clock-only one-second crossing becomes a timing warning. Other
guards remain. The legacy v2/v3 audit paths and every old result remain unchanged.

The new frozen environment and tests are documented in the
[implementation report](../docs/validation/2026-11-11-whole-run-qualification.md).
The resumed AC/open-lid/awake preflight passed in the exact launcher context.
All six cases and independent audits passed; services are reset/stopped. See the
[execution report](../docs/validation/2026-11-11-whole-run-qualification-execution.md).
The new allowance is now **consumed**. Its historical command was:

```sh
./fixture.sh batch plans/validation-readiness.json \
  --qualification-record plans/qualification-whole-run-v1.json
```

Do not rerun this consumed suite. The exclusive qualification claim prevents
repeats; local attempts remain 1 and
explicit cumulative counts retain all same-parent predecessors. Stop at the first
invalid/incomplete case during authorized collection.
This command authorizes no research collection or fitting/calibration.

## Local checks without Docker

Use Python 3.12 (the container version); Python 3.9 cannot parse the fixture's
type annotations. Alternatively, `./fixture.sh test` runs pytest in the container.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
python -m compileall -q fixture
sh -n fixture.sh
```

Unit checks cover validation, reproducible workload choices, bounded traffic,
log boundaries across simulated restart files, native-record preservation,
malformed/empty output, metadata leakage, independent lock proof, application
exception handling, and cleanup on injection failure/cancellation. They mock
external services and do not replace the real Docker integration command.

`./fixture.sh stop` stops only these services and retains evidence/data. To remove
this fixture completely, after saving any wanted exports:

```sh
docker compose down --volumes --remove-orphans
```

This deletes only this Compose project's containers/volumes; no Docker prune is
used. The host `runs/` exports remain. Keep `.env`, `.venv`, and generated runs out
of source control. See `IMPLEMENTATION_REPORT.md` for checks actually executed.
