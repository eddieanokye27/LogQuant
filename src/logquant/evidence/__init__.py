"""Deterministic offline original-evidence selection; no LLM or ground truth access."""
from .contracts import EvidenceConfig, EvidencePayload, EvidenceResult
from .selection import payload_bytes, select_evidence

__all__ = ["EvidenceConfig", "EvidencePayload", "EvidenceResult", "payload_bytes", "select_evidence"]
