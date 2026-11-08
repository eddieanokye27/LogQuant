"""Offline training orchestration; the only modeling layer allowed to read eligibility metadata."""
from .orchestration import fit_manifest

__all__ = ["fit_manifest"]
