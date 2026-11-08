# TF-IDF and Isolation Forest contract v1

This milestone implements fitting and raw anomaly scoring, using the existing
[ingestion](ingestion.md) and [feature/window](features.md) contracts unchanged.
Only synthetic test models have been fitted. No alert threshold, research model,
performance result, evidence selector or evaluation metric is supplied.

## Boundary and training eligibility

`logquant.training.fit_manifest` is the fitting entry point. Its separate
orchestration module reads selection, collection assignments and verification
metadata. The numerical core receives prepared runs and provenance separately;
**only each window's `document` string enters TF-IDF**. IDs, paths, timestamps,
split assignments, labels, metadata and reports are never appended to ML text.
Diagnostic exception types and SQLSTATEs remain ordinary diagnostic content.

Selection JSON has exactly these fields (paths relative to the selection file):

```json
{
  "schema_version": 1,
  "purpose": "synthetic-test",
  "runs": [
    {"run_id": "synthetic-train-1", "split": "train", "directory": "collection/synthetic-train-1"}
  ]
}
```

`purpose` can later be `research`; validation collections are always ineligible.
The example generator supplies three synthetic runs and explicitly mock
verification metadata. Its artifacts are marked `synthetic-test-only`.

For each selected run, require:

- A symlink-free run directory whose basename matches its unique run ID, both
  required diagnostic files, and complete, error-free ingestion.
- A sibling collection `manifest.json` with schema 1, matching purpose and
  `status: complete`. Accepted assignments must have unique run IDs and slot IDs
  across all splits in that collection. The selected run must have exactly one
  assignment, `split: train`, `status: valid`, no invalidity reasons and an
  explicit null `config.category`. Per-window selections are forbidden.
- `evaluation/ground_truth.json` with matching ID, `valid: true`, no invalidity
  reasons, `designation: healthy`, explicit null `fault_category` and
  `config.category`, timezone-aware `recovery_verified_at`, and diagnostic counts
  matching ingestion. `simulation` must match the synthetic/research purpose.
- No repeated selected identities, resolved directories, accepted slots or
  identical pairs of diagnostic-file hashes, even under different run names.

Every selected run is checked before any fitting. Extra fixture metadata remains
evaluation-only. JSON duplicate keys and non-finite values are rejected. Metadata
and diagnostic paths use the existing no-symlink file boundary. These checks
validate recorded verification, not its authenticity: trusted local collection
records remain necessary. Renamed/edited duplicates across external collections
cannot be identified reliably from logs alone.

## Deterministic fitting

Sort runs by ID and windows chronologically using the existing canonical grid.
Reject incomplete, altered or mixed-run window structures. Never cross runs.
The first/last observed buckets have unknown coverage and are excluded from
fitting, including both flags when only one bucket exists. Interior empty
documents are excluded rather than treated as normal behavior.

TF-IDF fits the remaining healthy training window documents, then Isolation
Forest fits their nonzero vectors. Sparse CSC fitting and CSR scoring are
supported and regression-tested with scikit-learn 1.7.2; the implementation never
densifies the matrix. See the official [Isolation Forest API](https://scikit-learn.org/1.7/modules/generated/sklearn.ensemble.IsolationForest.html)
and [TF-IDF API](https://scikit-learn.org/1.7/modules/generated/sklearn.feature_extraction.text.TfidfVectorizer.html).

Validated configuration defaults:

```json
{
  "seed": 42,
  "n_estimators": 100,
  "max_samples": 256,
  "max_features": 5000,
  "ngram_min": 1,
  "ngram_max": 2,
  "edge_policy": "exclude-fit-score-with-coverage-flag"
}
```

`max_features` caps TF-IDF vocabulary, not forest feature subsampling. Integer
types/ranges and ordered n-grams are checked; unknown options are rejected.
TF-IDF uses case-sensitive default word tokenization, unigrams/bigrams, L2
normalization, sublinear term frequency, no stop words, `min_df=1`, `max_df=1.0`
and float64. Forest uses one job, no bootstrap, all available features and
`min(max_samples, usable_windows)` samples. The fixed seed and sorted input make
repeated fitting deterministic in the qualified environment. The library's
`contamination="auto"` internal offset is unused; neither `predict` nor
`decision_function` supplies an alert decision.

## Missing information and inference

| Input condition | Training | Inference |
| --- | --- | --- |
| Empty document | Exclude; audit `empty_document` | Null score, `empty_document` |
| Nonempty document with zero TF-IDF vector | Exclude from forest; audit `zero_vector` | Null score, `zero_vector` |
| First/last observed bucket | Exclude; audit `excluded_edge` | Score if nonzero, always `coverage_unknown: true` |
| All empty/edge-only data or empty vocabulary | Explicit fit failure | No refitting or replacement vocabulary |
| Fewer than two nonzero eligible training windows | Explicit fit failure | Not applicable |
| No application events | Explicit ingestion failure | Explicit ingestion failure, no health assertion |
| Missing/malformed/incomplete inputs | Explicit failure | Explicit failure |

Vocabulary capping can make some training documents zero even though they were
used to fit IDF; the audit records this distinction. Inference always uses the
saved vocabulary, IDF and forest, with `transform` and `score_samples` only.
Out-of-vocabulary terms are ignored. A partially known document can score despite
unseen diagnostic terms; nonzero does not mean fully represented. Zero vectors
and absence of observations must never be interpreted as healthy.

The reported `anomaly_score` is **negative `score_samples`**, so higher values
are more anomalous relative to the fitted reference. Scores are not probabilities.
Every window retains its run/window identity, UTC interval and original evidence
line IDs. Empty windows remain present. Unknown leading/trailing event-free
periods are never invented. Edge scores remain coverage-unknown, not full
30-second exposure measurements. `alert_threshold` is explicitly null.

Threshold selection is a separate future development-only step; freeze it before
final testing. Neither synthetic score ordering nor the library's default cutoff
demonstrates fault detection accuracy.

## Artifacts and environment

`model.pkl` saves vectorizer, forest and typed metadata together.
`model-metadata.json` is a readable copy with the payload SHA-256. It includes
configuration, preprocessing versions, selection/assignment/verification/source
hashes, per-window fit/exclusion audit, run provenance, effective sample count,
vocabulary size, implementation fingerprint and all installed dependency versions.
These identifiers are provenance, never vectorizer input. Model identity hashes
the logical fitting manifest; reordering/reformatting metadata may change identity
without changing numerical results. Absolute provenance paths also affect identity.

**Load only trusted project models.** Pickle deserialization can execute code.
Checksums detect corruption but do not authenticate artifacts. Loading rejects
dependency/Python/platform, analysis-lock or implementation mismatches before
deserialization; this intentionally prevents silent cross-version reuse. Source
changes require reviewed refitting rather than bypassing compatibility checks.

The complete 17-distribution analysis/build/test lock is
`requirements-analysis.lock`; `analysis-lock-info.json` records resolved wheels
and hashes. It targets CPython 3.13.5, Apple Silicon, with numerical wheels
requiring macOS 14+. It was qualified on macOS 15.5. Recreate from repository root:

```sh
/opt/anaconda3/bin/python -m venv .venv
.venv/bin/python -m pip install --require-hashes --no-deps -r requirements-analysis.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

Use an interpreter matching the qualified Python version. This is a platform
wheel lock, not a portable multi-platform lock. Other platforms/Python versions
need their own reviewed lock and verification. `scripts/lock_analysis.py` captures
a reviewed resolved environment and refuses overwrites; it is not an implicit
dependency updater. The frozen Docker fixture environment is independent.

## CLI: synthetic example only

Run once from repository root, after the installation above:

```sh
.venv/bin/python scripts/make_synthetic_detector_example.py results/detector/synthetic-only-example
.venv/bin/logquant-detector fit results/detector/synthetic-only-example/training-selection.json \
  --artifact-dir results/detector/synthetic-test-only-model
.venv/bin/logquant-detector score results/detector/synthetic-only-example/synthetic-score/diagnostic \
  --run-id synthetic-score --artifact-dir results/detector/synthetic-test-only-model \
  --output-dir results/detector/synthetic-only-scores
```

Optional `fit --config config.json` accepts the configuration above. Scoring reads
only the two diagnostic files, never split/validity metadata. It writes
`scores.jsonl` plus `score-report.json` with counts, input hashes and null threshold.
Both commands reject existing output files unless explicitly given `--overwrite`.
Outputs must stay outside diagnostic sources; score outputs also stay outside
the model directory. Paths reject symlinks and nonregular files. Per-file writes
are atomic; multi-file groups are not crash-atomic. CLI exit 2 means ingestion
failure with a detailed report; other controlled failures exit 1.

This demonstration uses mock eligibility flags, not independently executed
experiments. Generated inputs/models remain ignored beneath `results/detector/`.
No readiness-validation case is eligible for research fitting.
