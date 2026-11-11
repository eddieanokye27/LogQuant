"""TEST-ONLY: original logs with manually assigned scores and shared candidate grouping.

No detector found these incidents; scores are assigned solely to test selection.
No model fitting, calibration, real fault injection or research data is involved.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from logquant.detection.contracts import ScoringResult, WindowScore
from logquant.features import prepare_ingestion
from logquant.ingestion import load_diagnostic_directory
from logquant.thresholds.alerts import classify
from logquant.thresholds.contracts import DetectorIdentity, SCORE


RUN_ID = "synthetic-evidence-01"


def create_example(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=False)
    diagnostic = output/"diagnostic"
    diagnostic.mkdir()
    base = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    offsets = [0, 29.999999, 30, 59.999999, 60, 89.999999, 90, 119.999999,
               120, 149.999999, 150, 179.999999, 180, 209.999999, 210]
    rows = [dict(timestamp=(base+timedelta(seconds=offset)).isoformat(), service="application",
                 severity="INFO", event="request", message=f"ordinary operation {i}", request_id=f"opaque-request-{i}",
                 route="/products/123/quote", status=200) for i, offset in enumerate(offsets)]
    rows[4].update(severity="ERROR", message="café: canceling statement due to lock timeout\noriginal second line",
                   exception_type="LockNotAvailable", status=500)
    (diagnostic/"application.jsonl").write_bytes("".join("  "+json.dumps(r, ensure_ascii=False)+" \r\n" for r in rows).encode())
    pg = dict(timestamp=(base+timedelta(seconds=60)).isoformat(), error_severity="ERROR",
              message="canceling statement due to lock timeout", state_code="55P03", detail="native detail")
    other = dict(timestamp=(base+timedelta(seconds=90)).isoformat(), error_severity="ERROR",
                 message="permission denied for table orders", state_code="42501")
    (diagnostic/"postgresql.jsonl").write_text("".join(json.dumps(r)+"\n" for r in (pg, pg, other)))
    ingestion = load_diagnostic_directory(diagnostic)
    run = prepare_ingestion(ingestion, run_id=RUN_ID)
    identity = DetectorIdentity(model_id="test-only-manually-assigned-scores", usage="synthetic-test-only",
                                payload_sha256="not-a-fitted-model")
    for name, indices in (("alerts", {2, 3}), ("no-candidate-alerts", set()), ("two-candidate-alerts", {2, 3, 5})):
        directory = output/name
        directory.mkdir()
        scores = ScoringResult(model_id=identity.model_id, model_usage=identity.usage, score_definition=SCORE,
            input_sha256={s.source: s.sha256 for s in ingestion.report.sources},
            scores=tuple(WindowScore(run_id=RUN_ID, window_id=w.window_id, start=w.start, end=w.end,
                line_ids=w.line_ids, status="scored", anomaly_score=0.9 if w.index in indices else 0.1,
                coverage_unknown=w.first_observed_bucket or w.last_observed_bucket) for w in run.windows))
        result = classify(scores, identity, 0.5)
        report = result.model_dump(mode="json", exclude={"windows", "candidates"})
        report.update(candidates=len(result.candidates), input_sha256=scores.input_sha256,
                      note="TEST-ONLY manually assigned scores; no detector discovery.")
        (directory/"alerts.jsonl").write_text("".join(w.model_dump_json()+"\n" for w in result.windows))
        (directory/"candidates.jsonl").write_text("".join(c.model_dump_json()+"\n" for c in result.candidates))
        (directory/"alert-report.json").write_text(json.dumps(report, indent=2)+"\n")
    (output/"tight-config.json").write_text(json.dumps(dict(max_records=9, max_payload_bytes=262144))+"\n")
    (output/"impossible-config.json").write_text(json.dumps(dict(max_payload_bytes=1))+"\n")
    (output/"TEST-ONLY.txt").write_text(__doc__+"\n")
    return diagnostic


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(create_example(parser.parse_args().output))
