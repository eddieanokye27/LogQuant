# LogQuant

An Autonomous Multi-Agent System for Log-Based Root Cause Analysis and Anomaly Detection.

The local evaluation fixture, structured ingestion, feature normalization and
30-second windows are implemented. Analysis supports healthy-only TF-IDF/Isolation
Forest, development-only frozen thresholds/candidate grouping and deterministic
bounded offline evidence bundles. Typed Triage/RCA contracts, deterministic
prompts and response validation have offline CLIs. A minimal local LangGraph
workflow and OpenAI adapter are verified with mocked HTTP and one complete
synthetic live smoke. The uniform-v4 training/development stage now has nineteen
accepted runs and a research detector fitted on six new healthy training runs.
Development calibration is infeasible under the unchanged recall/zero-healthy-
candidate objective; no research alert threshold exists. The dashboard remains
future work. See the [actual research report](docs/validation/2026-11-12-research-v4-01.md).
See [current status](docs/status.md) and the [architecture](docs/architecture.md).

For ingestion, use Python 3.11+ in its own environment (this Mac has Python 3.13
at `/opt/anaconda3/bin/python`; the system Python 3.9 is insufficient):

```sh
/opt/anaconda3/bin/python -m venv .venv
.venv/bin/python -m pip install --require-hashes --no-deps -r requirements-analysis.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python scripts/cache_analysis_tokenizer.py
.venv/bin/python -m pytest -q
.venv/bin/logquant-ingest \
  logquant-fixture/validation-runs/79cc295efae946348240d6893749d1fc/30da5d5ed82e493d8669c78b8631df02/diagnostic \
  --output-dir results/ingestion/example-healthy
```

The CLI reads only `application.jsonl` and `postgresql.jsonl`, rejects symlinks,
and writes normalized records plus a separate report outside the source. It
fails explicitly on input errors by default. `--permissive` allows partial
records with an incomplete report and exit 2; `--overwrite` explicitly replaces
existing ingestion outputs. See the [input/API/CLI contracts](docs/ingestion.md).

The [fixture](logquant-fixture/README.md) has its own frozen Docker/dependency
environment. Installing LogQuant does not rebuild or change that environment.

To build separate feature documents and observed-event windows for one run:

```sh
RUN_ID=30da5d5ed82e493d8669c78b8631df02
.venv/bin/logquant-features \
  "logquant-fixture/validation-runs/79cc295efae946348240d6893749d1fc/$RUN_ID/diagnostic" \
  --run-id "$RUN_ID" --output-dir results/features/example-healthy
```

If updating an existing editable installation, first run
`.venv/bin/python -m pip install --no-deps --no-build-isolation -e .` to register commands.
See the [feature/window contract](docs/features.md) for explicit normalization
rules, epoch alignment, empty windows and unknown collection-edge coverage.
These artifacts do not contain TF-IDF vectors or anomaly scores.

The complete analysis lock targets Python 3.13.5 on Apple Silicon/macOS 15+;
other platforms need a separate reviewed lock. See the [detector contract and
fit/score commands](docs/detection.md) for a synthetic-only demonstration,
whole-run eligibility checks, null scores for missing information, and trusted
model loading. Higher anomaly scores are more unusual, not probabilities;
one healthy-only research model is fitted, but no usable research threshold was
selected. The [current real-data report](docs/validation/2026-11-12-research-v4-01.md)
records all collection outcomes, manual replacement history and unsuccessful
development calibration. The earlier partial collection remains preserved. The separate
[threshold policy and CLI](docs/thresholds.md) demonstrate selection, retained
infeasible outcomes and frozen classification using synthetic fixtures only.

The [evidence contract and CLI](docs/evidence.md) select original diagnostic
records around frozen candidate groups, with up to 60 seconds of context per
side, exact per-bundle byte/record budgets, stable citations and separate omission
audits. The synthetic demonstration uses explicitly manually assigned scores;
no LLM calls or research experiments are involved.

See [offline agent contracts and commands](docs/agent-contracts.md) for strict
status/rank/citation validation, curated omission summaries, versioned prompts,
explicit input/output budgets and preserved repair attempts. Mock validation
does not measure diagnosis accuracy or prompt-injection resistance.

The [execution workflow/setup](docs/workflow.md) documents the real SDK/graph
offline mode, 53-distribution lock, token-estimation assumptions and a prepared
two-call synthetic live smoke command. Three retained attempts ended in HTTP
429, Triage truncation, then validated Triage/RCA completion with the separate
post-failure v2 budget. See the [live report](docs/validation/2026-11-10-live-smoke-three.md)
for usage and semantic review. This is not research performance or a frozen
research configuration.

The [continuity investigation](docs/validation/2026-11-11-continuity-investigation.md)
retains the blocked research campaign, bounded nonresearch diagnostics and a
proposed recovery decision. Timing metadata was improved, but the mechanism
remains unresolved and that earlier fixture revision failed qualification;
no additional research execution is authorized by that proposal.

The preceding [v3 qualification](docs/validation/2026-11-11-v3-qualification-execution.md)
remains stopped/exhausted after two valid cases and one clock-invalid case.
The approved [Mac whole-run amendment](docs/whole-run-protocol.md) is implemented,
synthetically tested and frozen/archived separately. Its single six-case
nonresearch qualification and independent audits passed in the final environment;
the allowance is consumed. See the [execution report](docs/validation/2026-11-11-whole-run-qualification-execution.md)
and [proposed replacement campaign](docs/validation/2026-11-11-replacement-campaign-proposal.md).
That replacement campaign was subsequently approved and completed for s001–s019;
see the [results](docs/validation/2026-11-12-research-v4-01.md). All 23 test slots
remain pending, and no further collection is authorized by that completed task.
