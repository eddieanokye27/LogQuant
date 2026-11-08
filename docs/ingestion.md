# Diagnostic ingestion contract v1

LogQuant 0.1.0 ingests the fixture's **structured JSONL only**, using Python JSON
parsing and source-specific Pydantic contracts. It does not use Drain or perform
message normalization, feature extraction, windowing, detection or diagnosis.
The proposal's broader documented text/upload formats remain future adapters;
this milestone does not claim arbitrary log-format support.

## Input boundary

Pass one explicit diagnostic directory. Only these two literal filenames are
opened, in this order: `application.jsonl`, `postgresql.jsonl`. Both must exist.
There is no directory discovery, recursion, parent lookup, run-manifest loading,
or automatic addition of `diagnostic/`. Passing a run root does not discover its
logs. Extra files and subdirectories, including evaluation metadata, are ignored.
Keep each run separate; the loader does not combine runs or assign partitions.

Every directory component and each input file is opened without following
symlinks, using POSIX directory file descriptors and `O_NOFOLLOW`. Files must be
regular files; directory/FIFO/device inputs are rejected. This also checks
symlinks in ancestors and before `..` components. Use a real path, not a symlink
alias (including OS aliases such as `/tmp` on macOS). macOS/Linux are supported;
platforms without the required POSIX flags fail explicitly rather than using an
unsafe fallback. Opened directory descriptors pin the input boundary during reads.

Inputs are offline immutable exports. All content remains untrusted diagnostic
data, not agent instructions. An allowlist cannot establish the authenticity of
file contents or prove absence of every possible metadata-contamination channel.

## JSONL envelope

- UTF-8, one JSON object per physical LF-delimited line. CRLF and a final line
  without a terminator are supported and preserved exactly in `raw_text`.
- Blank/whitespace-only lines are errors; they are counted, never skipped.
  Pretty-printed multiline objects are not JSONL and are not supported.
- Duplicate object keys (including nested keys), non-finite numbers, overflowing
  float values, invalid UTF-8 and unpaired Unicode surrogates are rejected.
- Unknown fields are retained in `raw_record`; they are not flattened into
  invented common fields. Required/known optional fields are type-checked without
  converting strings to numbers or booleans to integer statuses.
- An empty PostgreSQL file is valid. An empty application file is incomplete,
  because the fixture contract requires application evidence. An absent file
  is a separate file-level error, even when an empty file of that name is valid.

## Source-specific mappings

### Application

| Input field | Requirement | Common field / behavior |
| --- | --- | --- |
| `timestamp` | Required nonempty string | Aware UTC `timestamp` |
| `service` | Required literal `application` | `service`, unchanged |
| `severity` | Required: DEBUG, INFO, WARNING, ERROR or CRITICAL | `severity`, unchanged |
| `message` or `event` | At least one nonempty string; null counts as absent | Use exact `message` when present, otherwise exact `event`; record choice in `message_field` |
| `exception_type` | Optional nonempty string/null | `exception_type`; when present, also expose original `message` as `exception_message` if available |
| `event`, `request_id`, `method`, `route` | Optional nonempty strings/null | Corresponding common fields, unchanged |
| `status` | Optional integer 100–599/null | `status`; boolean/string rejected |
| `duration_ms` | Optional finite nonnegative number/null | Numeric `duration_ms` |

For ordinary fixture requests, `message == "request"` and `message_field ==
"event"`; method/route/status stay separately available. No synthetic sentence
is assembled. Error records keep their full original message (including embedded
newlines and SQL excerpts), even when `event` is also present. An exception type
without a message is retained, with `exception_message=null`; missing details
are never fabricated. A message without an exception type is still preserved.

### PostgreSQL

| Input field | Requirement | Common field / behavior |
| --- | --- | --- |
| `timestamp` | Required nonempty string | Aware UTC `timestamp` |
| `error_severity` | Required: DEBUG1–DEBUG5, INFO, NOTICE, WARNING, ERROR, LOG, FATAL or PANIC | `severity`, unchanged |
| `message` | Required nonempty string | Exact `message`, `message_field="message"` |
| `state_code` | Optional five uppercase alphanumeric characters/null | `sqlstate`, unchanged |
| `detail`, `hint`, `context` | Optional strings/null | Corresponding common fields, unchanged |

Native PostgreSQL JSON has no service field; the adapter maps this known source
to `service="postgresql"`. PID, native `line_num`, session identifiers, other
timestamps and all other native fields remain in `raw_record`. SQLSTATE does not
change severity or imply a category: a LOG record with `55P03` remains LOG.
No exception type or fault label is inferred from database messages or SQLSTATE.

## Timestamp contract

Both adapters support calendar timestamps with full seconds, optional 1–6
fractional digits, and explicit `Z` or numeric `±HH:MM` offset. `T` or a space
separates date/time. PostgreSQL also supports its native suffix ` UTC`, for
example `2026-11-07 22:00:06.362 UTC`. Calendar dates/offsets must be valid.

Naive timestamps are errors: no host timezone, DST rule or run metadata is used
to guess an offset. Other timezone abbreviations and higher precision are
unsupported. Normalize only the common timestamp to an aware UTC datetime;
the original timestamp string and native auxiliary timestamps remain untouched
inside `raw_record` and `raw_text`. Serialized common timestamps use UTC `Z`.

## Common record and ordering

`LogRecord` contains timestamp, service, severity, message, `message_field`, source
filename, original one-based **physical** line number, `line_id`, original
`raw_record` and `raw_text`, plus the optional fields above. Raw text includes its
original terminator/spacing; encoding it as UTF-8 reproduces the source line.
JSON output escapes that text as a JSON string; decoding restores it exactly.
`raw_record` is the original parsed JSON object, not a schema dump with added
defaults. Treat these raw values as read-only. Original files are never rewritten.

IDs are assigned before sorting: `application.jsonl:17` or `postgresql.jsonl:8`.
They are unique **within one run** and independent of message values, timestamps,
file paths, sorting or permissive exclusions. They are not globally unique across
runs. Native PostgreSQL `line_num` is not the physical provenance counter.

Ordering is `(UTC timestamp, source order, physical line number)`, with application
before PostgreSQL on ties. Duplicate real events remain distinct records with
distinct physical IDs; nothing is deduplicated.

## Errors and API

```python
from logquant.ingestion import IngestionError, load_diagnostic_directory

result = load_diagnostic_directory(diagnostic_path)  # strict by default
records = result.records
```

Strict mode gathers all discovered issues, then raises `IngestionError`; its
`.result.report` retains the errors and `.result.records` retains partial valid
records for inspection. It does not silently turn failure into an empty/healthy
result. For explicitly requested partial ingestion:

```python
result = load_diagnostic_directory(diagnostic_path, permissive=True)
assert not result.complete  # if any input issue occurred
```

Every issue supplies source filename, physical line number and reason, with kind
`json`, `schema`, `timestamp`, `timezone`, `encoding`, `io` or `boundary`.
File/directory-level issues use `line_number=null` because no physical line exists.
Permissive mode returns valid records alongside all issues and marks the result
incomplete; it never follows rejected symlinks. Invalid lines remain in the
untouched original input for inspection, rather than receiving fabricated records.
Per-file reports distinguish read/empty, missing, rejected and unreadable inputs,
and record physical/valid counts, bytes read and the input SHA-256. An I/O-failed
read's hash/counts describe only the bytes actually read, with state `unreadable`.

## CLI and artifacts

From the repository root, on this machine (system Python 3.9 is unsupported):

```sh
/opt/anaconda3/bin/python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/logquant-ingest \
  logquant-fixture/validation-runs/79cc295efae946348240d6893749d1fc/30da5d5ed82e493d8669c78b8631df02/diagnostic \
  --output-dir results/ingestion/example-healthy
```

Python 3.11+ is supported; the executed environment is separate Python 3.13.
`python -m logquant.ingestion` is an equivalent entry point after installation.
Dependencies live in root `pyproject.toml`; fixture locks/images are not modified.

Outputs are `records.jsonl` and separate `ingestion-report.json`. The destination
must be outside the source directory. Output directory components/files may not
be symlinks. Existing reserved output names are rejected unless `--overwrite` is
explicit. Individual files are staged and installed without truncating source
inodes; output pairs are not a crash-atomic transaction. Use one destination per
attempt/run and do not consume an artifact until the command has completed.

| Exit | Meaning | Written artifacts |
| --- | --- | --- |
| 0 | Complete ingestion | Records and complete report |
| 2 | Strict input failure | Error report only, no partial normalized records |
| 2 with `--permissive` | Incomplete ingestion explicitly requested | Valid records and incomplete/error report |
| 1 | Output/CLI operation cannot complete | Error on stderr; no successful completion claim |

Both modes return 0 for valid complete inputs. With `--overwrite`, a strict input
failure removes any older `records.jsonl` from that destination so it cannot sit
beside a new failure report. Permissive outputs must not be treated as complete
training/evaluation data. This module is an in-memory offline parser, not a live
collector or an unlimited-upload service.
