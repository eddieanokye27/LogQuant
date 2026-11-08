"""Evaluation-only collection plans; never supplied to diagnostic consumers."""
from collections import Counter
import hashlib
import json

from fixture.core import CATEGORIES, Config


def validate_plan(payload: dict) -> list[tuple[dict, Config]]:
    """Validate a complete plan without touching Docker or creating run files."""
    if payload.get('schema_version') != 1 or payload.get('purpose') not in ('research', 'validation'):
        raise ValueError('Expected version 1 research or validation plan')
    if payload.get('max_attempts_per_slot') != 3:
        raise ValueError('Protocol permits three attempts per slot')
    rows = [(item, Config(**item['config'])) for item in payload['runs']]
    slots = [item['slot_id'] for item, _ in rows]
    seeds = [cfg.seed for _, cfg in rows]
    if not slots or len(slots) != len(set(slots)) or any(not isinstance(s, str) or not s for s in slots):
        raise ValueError('Plan requires unique nonempty slot IDs')
    if len(seeds) != len(set(seeds)):
        raise ValueError('Plan seeds must be unique')
    if any(cfg.baseline + cfg.exposure + cfg.recovery != 300 for _, cfg in rows):
        raise ValueError('Each planned run requires 300 seconds of traffic phases')
    counts = Counter((item['split'], cfg.category) for item, cfg in rows)
    if payload['purpose'] == 'validation':
        expected = Counter({('unassigned', category): 1 for category in (None, *CATEGORIES)})
    else:
        expected = Counter({('train', None): 6, ('development', None): 3, ('test', None): 3})
        expected.update({('development', category): 2 for category in CATEGORIES})
        expected.update({('test', category): 4 for category in CATEGORIES})
    if counts != expected:
        raise ValueError('Plan counts/splits do not match the agreed protocol')
    return rows


def plan_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def plan_summary(payload: dict) -> dict:
    rows = validate_plan(payload)
    return {'purpose': payload['purpose'], 'plan_sha256': plan_hash(payload),
            'runs': len(rows), 'scheduled_seconds': sum(
                cfg.baseline + cfg.exposure + cfg.recovery for _, cfg in rows),
            'splits': dict(Counter(item['split'] for item, _ in rows)),
            'healthy': sum(cfg.category is None for _, cfg in rows)}
