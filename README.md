# LogQuant

An Autonomous Multi-Agent System for Log-Based Root Cause Analysis and Anomaly Detection.

The local evaluation fixture, structured diagnostic ingestion, feature-only
normalization, deterministic 30-second windows, and healthy-only TF-IDF/Isolation
Forest fitting with frozen raw scoring are implemented. Research collection,
threshold selection, evidence selection, agents and the dashboard remain future work.
See [current status](docs/status.md) and the [architecture](docs/architecture.md).

For ingestion, use Python 3.11+ in its own environment (this Mac has Python 3.13
at `/opt/anaconda3/bin/python`; the system Python 3.9 is insufficient):

```sh
/opt/anaconda3/bin/python -m venv .venv
.venv/bin/python -m pip install --require-hashes --no-deps -r requirements-analysis.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
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

The complete analysis lock targets Python 3.13.5 on Apple Silicon/macOS 14+;
other platforms need a separate reviewed lock. See the [detector contract and
fit/score commands](docs/detection.md) for a synthetic-only demonstration,
whole-run eligibility checks, null scores for missing information, and trusted
model loading. Higher anomaly scores are more unusual, not probabilities;
no alert threshold or research model has been fitted.
