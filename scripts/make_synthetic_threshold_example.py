"""Generate synthetic-only development fixtures, including mock verification flags.

The identical healthy/faulty feature documents intentionally make a zero-budget
objective infeasible. This tests policy mechanics, not fault detection ability.
Run the existing detector example generator/fit first, then this script and score
both development diagnostics with that single synthetic detector.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def create_example(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=False)
    collection = output/"collection"
    collection.mkdir()
    attempts, entries = [], []
    for index, name in enumerate(("synthetic-dev-healthy", "synthetic-dev-faulty")):
        directory = collection/name
        diagnostic = directory/"diagnostic"
        diagnostic.mkdir(parents=True)
        base = datetime(2026, 2, 1, tzinfo=timezone.utc)+timedelta(hours=index)
        rows = [dict(timestamp=(base+timedelta(seconds=i*30)).isoformat(), service="application",
                     severity="INFO", event="request", status=200, message="inventory read completed")
                for i in range(7)]
        (diagnostic/"application.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
        (diagnostic/"postgresql.jsonl").write_text("")
        category = None if not index else "Application exception"
        evaluation = directory/"evaluation"
        evaluation.mkdir()
        ground = dict(schema_version=2, run_id=name, designation="faulty" if index else "healthy",
            fault_category=category, valid=True, invalidity_reasons=[], simulation=True,
            config=dict(category=category), recovery_verified_at=rows[-1]["timestamp"],
            log_counts=dict(application=7, postgresql=0),
            observed_failure_interval=[rows[2]["timestamp"], rows[5]["timestamp"]] if index else None,
            note="MOCK verification for policy tests; identical feature text intentionally has no fault evidence.")
        (evaluation/"ground_truth.json").write_text(json.dumps(ground, indent=2)+"\n")
        attempts.append(dict(run_id=name, slot_id=f"synthetic-dev-slot-{index}", split="development",
                             status="valid", invalidity_reasons=[], config=dict(category=category)))
        entries.append(dict(run_id=name, split="development", directory=f"collection/{name}",
                            score_directory=f"scores/{name}"))
    (collection/"manifest.json").write_text(json.dumps(dict(schema_version=1, purpose="synthetic-test",
        status="complete", attempts=attempts), indent=2)+"\n")
    selection = output/"development-selection.json"
    selection.write_text(json.dumps(dict(schema_version=1, purpose="synthetic-test", runs=entries), indent=2)+"\n")
    for name, budget in (("demo-policy.json", 30.0), ("infeasible-policy.json", 0.0)):
        (output/name).write_text(json.dumps(dict(max_false_candidates_per_healthy_hour=budget,
            minimum_faulty_run_recall=1.0), indent=2)+"\n")
    (output/"TEST-ONLY.txt").write_text("Synthetic calibration mechanics only. Budget 30/hour is a permissive demo setting, not a proposal requirement.\n")
    return selection


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(create_example(parser.parse_args().output))
