"""Whole-run development policy v2. No diagnostic preparation or model fitting."""
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from logquant.ingestion.models import Contract
from logquant.detection.contracts import ScoringResult
from logquant.detection.core import sha256
from logquant.detection.storage import write_files
from .alerts import classify
from .contracts import CalibrationInput, Coverage, DetectorIdentity, SCORE
from .storage import canonical, read_bytes

POLICY = 'dev-whole-run-v2;recall-0.8;healthy-candidates-0;adjacent-v1;recorded-utc-first'


class WholeRunConfig(Contract):
    minimum_faulty_run_recall: Literal[0.8] = 0.8
    maximum_healthy_candidates: Literal[0] = 0


class WholeRunTrial(Contract):
    threshold: float = Field(allow_inf_nan=False)
    detected_faulty_runs: tuple[str,...]
    missed_faulty_runs: tuple[str,...]
    faulty_run_count: int
    recall: float
    healthy_candidates: int
    healthy_unscorable_runs: tuple[str,...]
    feasible: bool


class WholeRunArtifact(Contract):
    schema_version: Literal[2] = 2
    policy: Literal[POLICY] = POLICY
    artifact_id: str
    detector: DetectorIdentity
    score_definition: Literal[SCORE] = SCORE
    config: WholeRunConfig
    status: Literal['selected','unsuccessful']
    reasons: tuple[str,...]
    selection_sha256: str
    inputs: tuple[CalibrationInput,...]
    coverage: tuple[Coverage,...]
    timing_flags: dict[str,str]
    trials: tuple[WholeRunTrial,...]
    threshold: float | None = Field(allow_inf_nan=False)

    @model_validator(mode='after')
    def outcome(self):
        if (self.status=='selected')!=(self.threshold is not None):
            raise ValueError('Only successful whole-run calibration has a threshold')
        if self.status=='selected' and (self.reasons or not any(t.feasible and t.threshold==self.threshold for t in self.trials)):
            raise ValueError('Selected whole-run threshold lacks a feasible trial')
        if self.status=='unsuccessful' and not self.reasons:
            raise ValueError('Unsuccessful whole-run calibration requires reasons')
        return self


@dataclass(frozen=True)
class WholeRunCase:
    scores: ScoringResult
    faulty: bool
    timing_status: str = 'unassessable'


def seal(payload: dict) -> WholeRunArtifact:
    payload=dict(payload);payload.pop('artifact_id',None)
    provisional=WholeRunArtifact.model_validate_json(json.dumps({**payload,'artifact_id':'pending'}))
    return provisional.model_copy(update={'artifact_id':sha256(canonical(provisional.model_dump(mode='json',exclude={'artifact_id'})))})


def validate(artifact: WholeRunArtifact) -> WholeRunArtifact:
    checked=WholeRunArtifact.model_validate_json(artifact.model_dump_json())
    if seal(checked.model_dump(mode='json')).artifact_id!=checked.artifact_id:
        raise ValueError('Whole-run threshold checksum mismatch')
    return checked


def select(cases: list[WholeRunCase], detector: DetectorIdentity, config: WholeRunConfig,
           inputs: list[CalibrationInput], selection_sha256: str) -> WholeRunArtifact:
    config=WholeRunConfig.model_validate_json(config.model_dump_json())
    cases=sorted(cases,key=lambda c:c.scores.scores[0].run_id if c.scores.scores else '')
    initial=[classify(c.scores,detector,0) for c in cases]
    if len({r.coverage.run_id for r in initial})!=len(initial):raise ValueError('Duplicate whole-run calibration run')
    faulty=sum(c.faulty for c in cases)
    healthy=[r for c,r in zip(cases,initial) if not c.faulty]
    unscorable=tuple(r.coverage.run_id for r in healthy if not r.coverage.scorable_seconds)
    reasons=[]
    if not faulty:reasons.append('No eligible faulty development runs')
    if not healthy:reasons.append('No eligible healthy development runs')
    if unscorable:reasons.append('Healthy development runs lack finite interior scores')
    values=sorted({w.score.anomaly_score for r in initial for w in r.windows if w.state not in ('excluded_edge','unscored')})
    thresholds=[]
    if values:
        lower=math.nextafter(values[0],-math.inf)
        if not math.isfinite(lower):raise ValueError('Minimum score has no finite lower threshold')
        thresholds=[lower,*values]
    else:reasons.append('No usable interior development scores')
    trials=[]
    for threshold in thresholds:
        detected=[];missed=[];false=0
        for c in cases:
            result=classify(c.scores,detector,threshold);rid=result.coverage.run_id
            if c.faulty:(detected if result.candidates else missed).append(rid)
            else:false+=len(result.candidates)
        recall=len(detected)/faulty if faulty else 0
        trials.append(WholeRunTrial(threshold=threshold,detected_faulty_runs=tuple(detected),
            missed_faulty_runs=tuple(missed),faulty_run_count=faulty,recall=recall,
            healthy_candidates=false,healthy_unscorable_runs=unscorable,
            feasible=not reasons and recall>=config.minimum_faulty_run_recall and false==0))
    feasible=[t for t in trials if t.feasible]
    winner=min(feasible,key=lambda t:(-t.recall,t.healthy_candidates,-t.threshold)) if feasible else None
    if winner is None and not reasons:reasons.append('No threshold meets whole-run recall and zero-healthy-candidate constraints')
    return seal(dict(detector=detector.model_dump(mode='json'),config=config.model_dump(mode='json'),
        status='selected' if winner else 'unsuccessful',reasons=reasons,selection_sha256=selection_sha256,
        inputs=[i.model_dump(mode='json') for i in sorted(inputs,key=lambda x:x.run_id)],
        coverage=[r.coverage.model_dump(mode='json') for r in initial],
        timing_flags={r.coverage.run_id:c.timing_status for c,r in zip(cases,initial)},
        trials=[t.model_dump(mode='json') for t in trials],threshold=winner.threshold if winner else None))


def first_candidate(result):
    """Same choice for every run; no labels or diagnosis correctness are read."""
    return min(result.candidates,key=lambda c:(c.start,c.candidate_id),default=None)


def apply(result: ScoringResult, detector: DetectorIdentity, artifact: WholeRunArtifact):
    artifact=validate(artifact)
    if artifact.status!='selected' or artifact.detector!=detector:
        raise ValueError('Incompatible or unsuccessful whole-run threshold')
    return classify(result,detector,artifact.threshold)


def save(artifact: WholeRunArtifact, directory: Path, *, overwrite=False):
    artifact=validate(artifact)
    for item in artifact.inputs:
        if any(directory.resolve().is_relative_to(p.resolve()) for p in (Path(item.directory),Path(item.score_directory))):
            raise ValueError('Whole-run outputs must stay outside input sources')
    write_files(directory,{'calibration.json':(artifact.model_dump_json(indent=2)+'\n').encode()},overwrite=overwrite)


def load(directory: Path) -> WholeRunArtifact:
    return validate(WholeRunArtifact.model_validate_json(read_bytes(directory/'calibration.json')))


def assess_timing(alert: tuple[datetime,datetime] | None,
                  start: tuple[datetime,datetime] | None,
                  end: tuple[datetime,datetime] | None,
                  *, positive_overlap_guaranteed: bool = False) -> str:
    """Optional evaluation-only bounds; no bound is inferred from sampled clocks."""
    if any(x is None for x in (alert,start,end)):return 'unassessable'
    for lo,hi in (alert,start,end):
        if lo.tzinfo is None or hi.tzinfo is None or lo>hi:
            raise ValueError('Timing uncertainty requires ordered aware bounds')
    if start[0]>=end[1]:raise ValueError('No positive possible verified interval')
    if alert[1]<start[0] or alert[0]>=end[1]:return 'miss'
    if positive_overlap_guaranteed and alert[0]>=start[1] and alert[1]<end[0]:return 'definite'
    return 'unassessable'
