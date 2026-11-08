"""Frozen sparse scoring. Fitting is exposed only through training orchestration."""
from .contracts import DetectorConfig, ScoringResult, WindowScore
from .core import score_run
from .storage import load_trusted_artifact, save_artifact

__all__=["DetectorConfig","ScoringResult","WindowScore","score_run","save_artifact","load_trusted_artifact"]
