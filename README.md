# LogQuant

An Autonomous Multi-Agent System for Log-Based Root Cause Analysis and Anomaly Detection.

The local evaluation fixture and structured diagnostic ingestion are implemented.
Research collection, ML detection, agents and the dashboard remain future work.
See [current status](docs/status.md) and the [architecture](docs/architecture.md).

For ingestion, use Python 3.11+ in its own environment (this Mac has Python 3.13
at `/opt/anaconda3/bin/python`; the system Python 3.9 is insufficient):

```sh
/opt/anaconda3/bin/python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
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
