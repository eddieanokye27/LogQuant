"""Create TEST-ONLY logs and mock eligibility metadata; never a research dataset."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def create_example(output: Path) -> Path:
    output.mkdir(parents=True,exist_ok=False)
    collection=output/"collection";collection.mkdir()
    entries=[];attempts=[]
    base=datetime(2026,1,1,tzinfo=timezone.utc)
    for number in range(3):
        run_id=f"synthetic-train-{number+1}"
        directory=collection/run_id
        diagnostic=directory/"diagnostic";diagnostic.mkdir(parents=True)
        messages=["inventory read completed","order write committed","readiness operation succeeded"]
        records=[{"timestamp":(base+timedelta(minutes=number*10,seconds=i*30)).isoformat(),
                  "service":"application","severity":"INFO","event":"request","status":200,
                  "request_id":f"synthetic-request-{number}-{i}","message":messages[(i+number)%3]}
                 for i in range(5)]
        (diagnostic/"application.jsonl").write_text("".join(json.dumps(r)+"\n" for r in records))
        (diagnostic/"postgresql.jsonl").write_text("")
        evaluation=directory/"evaluation";evaluation.mkdir()
        ground={"schema_version":2,"run_id":run_id,"designation":"healthy","fault_category":None,
                "valid":True,"invalidity_reasons":[],"simulation":True,"config":{"category":None},
                "recovery_verified_at":records[-1]["timestamp"],"log_counts":{"application":5,"postgresql":0},
                "note":"Constructed unit/example eligibility flags; not independently executed fixture verification."}
        (evaluation/"ground_truth.json").write_text(json.dumps(ground,indent=2)+"\n")
        entries.append({"run_id":run_id,"split":"train","directory":f"collection/{run_id}"})
        attempts.append({"run_id":run_id,"slot_id":f"synthetic-slot-{number+1}","split":"train",
                         "status":"valid","config":{"category":None},"invalidity_reasons":[]})
    (collection/"manifest.json").write_text(json.dumps({"schema_version":1,"purpose":"synthetic-test",
        "status":"complete","attempts":attempts},indent=2)+"\n")
    selection=output/"training-selection.json"
    selection.write_text(json.dumps({"schema_version":1,"purpose":"synthetic-test","runs":entries},indent=2)+"\n")
    scoring=output/"synthetic-score/diagnostic";scoring.mkdir(parents=True)
    rows=[{"timestamp":(base+timedelta(days=1,seconds=offset)).isoformat(),"service":"application",
           "severity":"INFO","event":"request","status":200,"message":"inventory read completed with additional vocabulary"}
          for offset in (0,90,120)]
    (scoring/"application.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    (scoring/"postgresql.jsonl").write_text("")
    (output/"TEST-ONLY.txt").write_text("Synthetic implementation/example inputs and models only. Not research data or verified fault experiments.\n")
    return selection


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output",type=Path)
    print(create_example(parser.parse_args().output))
