# Feature and window contract v1

The feature stage creates new `FeatureRecord` and `FeatureWindow` objects from
complete, single-run ingestion. It never changes `LogRecord`, raw text, raw JSON,
original files, or prior ingestion artifacts. There is no fitting, vocabulary,
vectorization, scoring, incident selection or diagnosis in this stage.

Policies are versioned as `feature-v1` and `epoch-utc-30s-v1`. Keep the same
policies in future training and inference; any change requires an explicit
version/configuration decision before fitting or held-out evaluation.

## Feature fields and documents

Every feature record retains its run identity, original line ID, source filename,
physical line number and UTC timestamp as provenance. Its separate `fields`
dictionary contains only:

- Service, severity and message.
- Present event, method, route, request ID, exception type/message, SQLSTATE,
  detail, hint and context fields from ingestion.
- Present HTTP status, represented as its exact integer-code string.
- PostgreSQL `remote_host` and `application_name`, only when the original values
  are strings. These are the only raw-record extras used as feature fields.

`document` is canonical compact JSON of these fields with sorted keys and Unicode
preserved. It includes diagnostic field names to retain field boundaries and
structured request/error context. Embedded message newlines are JSON-escaped;
the underlying feature-field value retains them. Equal feature documents remain
separate observations; there is no deduplication.

**Only `document` is intended as future ML text.** Run IDs, line IDs, timestamps,
window IDs, physical line numbers and `changed_fields` are metadata and do not
enter it. Do not vectorize the entire serialized feature/window artifact. Raw
request bodies, original JSON dumps, PID/session metadata, durations and arbitrary
unknown fields are not automatically appended. Original evidence remains fully
available through ingestion and line-ID links.

## Explicit normalization rules

Rules operate on copies. Text transformations apply to message, exception message,
detail, hint, context, route and PostgreSQL remote host. They do not lowercase,
strip, reorder words, replace all numbers, or guess a cause.

| Rule | Scope | Replacement |
| --- | --- | --- |
| Structured request ID | Present `request_id` field, regardless of its value | `<REQUEST_ID>` |
| Tagged textual request ID | Case-insensitive `request_id=`, `request-id:`, `request id=`, or `requestid=` forms; optional matching quotes; token characters are alphanumeric/underscore/hyphen and dot-separated components | Replace the value with `<REQUEST_ID>`; retain the tag/quotes |
| Product route identifier | Standalone `/products/<decimal-or-canonical-UUID>` and `/products/<id>/quote` paths, optionally followed by a query/fragment | Replace the ID with `<ROUTE_ID>`; preserve endpoint/query text |
| Canonical UUID | Whole 8-4-4-4-12 hexadecimal UUID tokens | `<UUID>` |
| IPv6 literal | Candidate validated by `ipaddress.ip_address`, including compressed/mapped/scoped forms | `<IPV6>`; preserve external brackets/port/sentence punctuation |
| IPv4 literal | Dotted-quad candidate validated by `ipaddress.ip_address` | `<IPV4>`; preserve external port/punctuation |
| PostgreSQL process ID | `process <digits>` or `PID <digits>`, case-insensitive; also native `Process holding the lock:` and `Wait queue:` forms | Replace only PID values with `<PID>` |
| PostgreSQL object IDs | Native lowercase `relation <digits>` / `database <digits>` phrases | `<RELATION_ID>` / `<DATABASE_ID>` |

Textual order is tagged request ID, route ID, UUID, IPv6, IPv4, then native PG
contextual IDs. Native PG ID rules apply only to PostgreSQL records. Structured
request-ID replacement is then applied to its field. Applied transformations
are deterministic and idempotent for this contract; `changed_fields` lists changed
field names in sorted order.

Exception types, SQLSTATEs, service, severity, event, method, status and component
application names are kept exactly. Authentication/permission/lock wording,
user/table names, exit codes, errno, SQL placeholders/line numbers, ports and
elapsed-time values remain meaningful. For example, `42501` is not replaced
just because it is numeric, and LOG plus `55P03` remains LOG. The typed feature
does not infer an exception type or category from a message or SQLSTATE.

These are bounded syntax/context rules. Unknown route templates, query values,
bare non-UUID hashes and other numeric quantities are left alone. A version such
as `v1.2.3.4` is retained; a standalone IP-shaped value can be ambiguous and is
treated as an IP literal when valid. A number in a documented product-ID slot
is treated as an identifier, not a status code. The rules deliberately merge
identifier identity; they do not guarantee improved anomaly detection. Review
such choices on development data before freezing a later model.

## Window alignment and bounds

- Anchor: `1970-01-01T00:00:00Z` (UTC Unix epoch).
- Width: exactly 30 seconds. Bucket index is integer floor division of
  `(event timestamp - anchor)` by the 30-second interval. No floating-point epoch
  arithmetic is used, including before 1970 or at microsecond boundaries.
- Intervals are non-overlapping and half-open: `[start, end)`. An event at an
  exact boundary belongs to the new bucket, never the preceding one.
- Use the bucket containing the earliest event through the bucket containing
  the latest event, inclusive. Preserve **every** intervening bucket, even when
  empty. Empty windows have `line_ids=[]`, `document=""`, and `empty=true`.
- Keep first/last buckets at full width; do not trim them to event timestamps.
  `first_observed_bucket` and `last_observed_bucket` identify observation edges
  where collection coverage is unknown. A singleton bucket has both flags.
- Keep `observed_first_event` / `observed_last_event` as event timestamps, not
  collection boundaries. With no events the low-level API returns no windows
  and null observed endpoints. The CLI still requires complete ingestion, so
  empty application evidence fails at the loader boundary.

**Diagnostic events do not establish exact collection start/end.** Leading or
trailing periods without events cannot be inferred. No additional buckets are
created outside the observed bucket range, and no run start/end is invented.
Even if an edge event is exactly aligned, it does not establish collection
coverage of that entire bucket. Full-width grid edges are representation bounds,
not observed collection boundaries; do not multiply window counts by 30 to claim
actual healthy exposure or collection duration. Empty interior buckets mean no
retained events, not proof of health or continuous collection.

The report states `boundary_basis="observed-events-only"` and marks exterior
periods unknown. No ground truth, injection schedule, harness request file, run
manifest or other evaluation metadata is read to determine windows.

## Isolation, ordering and evidence links

`prepare_directory(path, run_id=...)` performs one strict allowlisted ingestion
for one explicit diagnostic directory. The run ID is supplied by the caller,
never inferred from a label or read from evaluation metadata. Use a distinct
identity for each actual run and keep outputs separate.

`prepare_ingestion(result, run_id=...)` accepts one complete ingestion result;
do not concatenate results from different directories before assigning an ID.
The low-level `build_windows(features, run_id=...)` rejects records whose run IDs
differ, duplicate evidence IDs, invalid physical-ID mappings and naive timestamps.
It cannot authenticate a caller's run labeling or determine whether input files
were previously concatenated; use the single-directory entry point.

Order records by `(UTC timestamp, application-before-PostgreSQL source order,
physical line number)`. Each window stores those original line IDs in order,
and its document joins the corresponding feature documents with a single newline.
Every input record appears in exactly one window. IDs remain those assigned by
ingestion before sorting; duplicate real events retain separate physical IDs.
Qualify an evidence lookup with `(run_id, line_id)` across runs.
Aligned edge windows from separate runs can overlap in absolute UTC time, or
share the same start, without sharing records. Never merge/deduplicate windows
across runs using timestamps alone; their run-qualified identities stay distinct.

Windows are ordered by start and have zero-based per-run indices. Window identity
is the explicit run ID plus the aligned start timestamp. No random/current-time
value, output-directory path or evaluation-derived value appears in artifacts,
so repeated execution with identical inputs, identity and policies yields the
same bytes.

## API, CLI and output

```python
from logquant.features import prepare_directory

run = prepare_directory(diagnostic_path, run_id="opaque-run-id")
features = run.features
windows = run.windows
```

From repository root, register the new console script in the existing analysis
environment without changing dependency versions:

```sh
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pytest -q
RUN_ID=30da5d5ed82e493d8669c78b8631df02
.venv/bin/logquant-features \
  "logquant-fixture/validation-runs/79cc295efae946348240d6893749d1fc/$RUN_ID/diagnostic" \
  --run-id "$RUN_ID" --output-dir results/features/example-healthy
```

`python -m logquant.features` is equivalent after installation. Python 3.11+
and the existing Pydantic dependency are used; there are no new dependencies.
For first-time setup see the [README](../README.md). The fixture environment
remains separate and frozen.

Artifacts are `features.jsonl`, `windows.jsonl` and `feature-report.json`.
The report includes policy versions/anchor, observed event extrema, counts,
unknown-exterior notice and the ingestion report/source hashes. It contains no
source evidence copies or inferred collection boundaries. The destination must
be outside the diagnostic directory. Directory/file symlinks are rejected and
existing output names require explicit `--overwrite`. Files are individually
staged, not a crash-atomic three-file transaction; consume them only after a
successful command.

Exit 0 means success; exit 2 emits the complete strict ingestion error report to
stderr and writes no feature artifacts; exit 1 indicates a feature/output error.
Incomplete/permissive ingestion is rejected rather than silently windowed.

This is an in-memory local pipeline. Long gaps allocate one empty window per
30 seconds; unlimited-size/live input and concurrent mutation are not supported.
No timestamp/latency/identifier feature policy here establishes causal diagnosis
or guarantees future detector performance.
