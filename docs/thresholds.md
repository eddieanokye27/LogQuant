# Development calibration and frozen alerts — policy v1

Policy defined before implementing selection on November 10, 2026. This adds
minimal classification/grouping, not evidence bundles or final evaluation tooling.
It preserves the proposal's first-alert-during-failure rule. All demonstrations
are explicitly synthetic-only; no research threshold or performance is claimed.

## Objective fixed before selection

Configuration must explicitly supply `max_false_candidates_per_healthy_hour`
(finite, >=0) and `minimum_faulty_run_recall` (finite, >0 and <=1). Neither number
is prescribed by the proposal. Require at least one healthy and one faulty
eligible development run, and positive scorable healthy observation duration.
Among thresholds satisfying both constraints, maximize faulty-run recall, then
minimize healthy candidate rate, then choose the highest threshold. Exact ties
are deterministic. All eligible faulty runs stay in the recall denominator,
including those with only null scores or excluded edges.

Candidate thresholds are the distinct usable interior scores across all selected
development runs, plus the next representable finite float below their minimum.
Use strict **score > threshold**, not >=. The maximum score is the no-alert
candidate. Search every threshold: adjacent grouping can make candidate counts
nonmonotonic. Extreme scores without a finite lower neighbor are rejected. Store
all trials, missed-run IDs, coverage and reasons if no useful threshold qualifies;
an unsuccessful result has no frozen threshold and cannot classify new runs.
No automatic budget relaxation, fallback cutoff or post-hoc run removal occurs.

## Shared grouping, alert time and missing coverage

Sort windows within each run. Preserve the complete epoch-aligned half-open
30-second grid; reject duplicate IDs, omitted buckets, malformed/null score
states and incompatible conventions. Only finite scored interior windows can
be `alert` or `below_threshold`. Nulls are `unscored`; coverage-unknown first/last
buckets are `excluded_edge` regardless of their score. Neither state means healthy.
They break grouping. Merge only touching flagged intervals within the same run;
never bridge a non-alert, null, excluded bucket or another run.

A candidate interval is [first flagged start, last flagged end). Its
`first_alert_at` is the **end of the first flagged window**, a nominal offline
window-availability timestamp, not actual processing time or incident onset.
Positive interval overlap requires `candidate.start < failure.end` and
`candidate.end > failure.start`. Separately, primary recall requires that overlap
and `failure.start <= first_alert_at < failure.end`. Thus overlap alone does not
establish proposal-aligned detection. Boundary contact is not positive overlap.
Use only the recorded `observed_failure_interval` (first/last sampled independent
failure observations), interpreted as half-open; never expand it to injection,
recovery, or all subsequent logs. This sampled interval is not proof of continuous
fault state and can conservatively miss boundary events.

Healthy false candidates are all grouped alerts in healthy development runs.
The denominator is **scorable healthy interior observation seconds / 3600**:
30 seconds per complete-grid, non-edge window with a usable score. Separately
report all interior grid seconds, null-score seconds, excluded-edge counts and
missed faulty runs. Edge buckets contribute zero duration; exact leading/trailing
collection coverage is unknown. These are diagnostic-observation grid measures,
not authenticated uninterrupted collection exposure. Event-free interior windows
remain unscored and contribute no denominator; frozen vocabulary can also reduce
coverage. This avoids silently crediting unscored time as healthy exposure.

## Eligibility, freezing and inference

A separate calibration orchestration layer accepts a complete selection manifest
of whole development runs and saved scores from one fixed trusted detector.
It checks completed collection assignments, verified validity/recovery, matching
healthy/fault category configuration, strict complete diagnostic ingestion,
source/count/window/score equality, unique identities/directories/content/slots,
and absence of training-run identity/content overlap. It verifies saved scores
against a frozen re-score; it never fits. Training/test assignments, validation
purpose, missing/conflicting assignments or incomplete exports are rejected.
Explicit `synthetic-test` collections are allowed only with a synthetic-test-only
detector and `simulation: true`; their verification flags are mock test fixtures.

Only this orchestration reads labels/verification. Detector and feature code stay
unchanged. Classifying diagnostics reads no ground truth and does no selection,
fitting or tuning. The JSON calibration artifact binds detector model ID and
payload fingerprint, score convention, exact policies, configuration, calibration
input hashes and selection results. It has a content checksum for integrity,
not authentication. Applying rejects unsuccessful, tampered or incompatible
artifacts. Model pickles must still come only from trusted project storage.
Output identities link windows, candidates and original evidence lines.

## Input/API and CLI

The development selection format is strict (no per-window selection):

```json
{
  "schema_version": 1,
  "purpose": "synthetic-test",
  "runs": [
    {
      "run_id": "synthetic-dev-healthy",
      "split": "development",
      "directory": "collection/synthetic-dev-healthy",
      "score_directory": "scores/synthetic-dev-healthy"
    }
  ]
}
```

Paths are relative to the selection file. Each run must have exactly one valid
development assignment in its parent `manifest.json`, no invalidity reasons,
matching run-directory basename, and valid `evaluation/ground_truth.json` with
matching counts, category/configuration, simulation purpose and aware recovery
timestamp. Accepted slot/run identities must be unique across the collection's
splits. Faulty verification requires one of the exact five categories and a
positive aware `observed_failure_interval` ending no later than recovery. A
healthy run cannot also declare a failure interval. Fault intervals outside
scorable coverage are retained as misses rather than removed. Collection status
must be complete; eligibility relies on trusted recorded verification, not
authentication of experimental truth.

Saved `scores.jsonl` and `score-report.json` use the existing detector CLI
contract. Missing windows/files, inconsistent reports, foreign models or altered
scores fail explicitly before searching. Labels cannot influence rescoring: only
the two diagnostic files enter unchanged ingestion/features and vectorization.
Source pairs are hashed to reject copied development or training content.
Edited/renamed content and unknown assignments outside the supplied collection
cannot be exhaustively detected. This is not a general data provenance service.

Install the new command without changing resolved dependencies:

```sh
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

The following reproduces the synthetic demonstration once from repository root;
use a fresh root path when rerunning input generators:

```sh
.venv/bin/python scripts/make_synthetic_detector_example.py results/thresholds/synthetic-v1/training
.venv/bin/python scripts/make_synthetic_threshold_example.py results/thresholds/synthetic-v1/development
.venv/bin/logquant-detector fit results/thresholds/synthetic-v1/training/training-selection.json \
  --artifact-dir results/thresholds/synthetic-v1/test-only-detector
for run_id in synthetic-dev-healthy synthetic-dev-faulty; do
  .venv/bin/logquant-detector score \
    "results/thresholds/synthetic-v1/development/collection/$run_id/diagnostic" \
    --run-id "$run_id" --artifact-dir results/thresholds/synthetic-v1/test-only-detector \
    --output-dir "results/thresholds/synthetic-v1/development/scores/$run_id"
done
.venv/bin/logquant-threshold calibrate results/thresholds/synthetic-v1/development/development-selection.json \
  --config results/thresholds/synthetic-v1/development/demo-policy.json \
  --detector-dir results/thresholds/synthetic-v1/test-only-detector \
  --output-dir results/thresholds/synthetic-v1/frozen
.venv/bin/logquant-threshold apply results/thresholds/synthetic-v1/development/collection/synthetic-dev-faulty/diagnostic \
  --run-id synthetic-dev-faulty --detector-dir results/thresholds/synthetic-v1/test-only-detector \
  --threshold-dir results/thresholds/synthetic-v1/frozen --output-dir results/thresholds/synthetic-v1/alerts
```

`demo-policy.json` explicitly allows **30 candidates/hour**, with minimum recall
1.0. This deliberately permissive mechanics example is not a research operating
point. The two development fixtures have identical feature text and mock faulty
verification. `infeasible-policy.json` instead allows zero false candidates and
cannot succeed on them. Run the same `calibrate` command with that config and a
new output directory to retain its unsuccessful outcome.

Calibration writes `calibration.json`, including all trials, recall denominators,
missed/overlapping/detected run IDs, coverage, objective configuration and input
fingerprints. Frozen application writes `alerts.jsonl`, `candidates.jsonl` and
`alert-report.json`, including threshold/detector identity, policy, source hashes
and original evidence links. Raw detector `alert_threshold` remains null; the
separate threshold artifact controls classification. `below_threshold` means only
the comparison failed, not that the window is healthy.

Exit codes: 0 success, 1 rejected input/artifact/output, 2 diagnostic ingestion
failure, **3 completed but unsuccessful calibration** (report still written).
Input rejection is not a failed threshold search; it exits before selection with
an explicit error. Existing outputs require `--overwrite`. Symlink/nonregular
paths are rejected. Outputs stay outside diagnostics/models and calibration run/
score sources; individual files are atomic but output groups are not crash-atomic.
The JSON checksum detects corruption, not malicious replacement. Existing pickle
version/source compatibility checks remain: old models are retained unchanged,
and new synthetic test models are fitted for changed source versions.

The API entry points are `thresholds.orchestration.calibrate_manifest` with a
trusted loaded detector/identity and explicit config, and ground-truth-free
`thresholds.apply_threshold` with a raw `ScoringResult`, detector identity and
successful compatible artifact. The score-grid checks and grouping function are
shared by both paths. Search is exhaustive over the small development score set;
it is not optimized for large arbitrary datasets.
