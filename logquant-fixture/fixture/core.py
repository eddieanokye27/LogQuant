"""Pure configuration, workload, and evidence-boundary helpers."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import math
import random
from pathlib import Path

CATEGORIES = ('Database unavailable', 'Invalid database credentials', 'Application exception',
              'Database permission denied', 'Database lock contention')

def now():
    return datetime.now(timezone.utc).isoformat()

@dataclass
class Config:
    baseline: float = 60
    exposure: float = 180
    recovery: float = 60
    rate: float = 5
    concurrency: int = 4
    seed: int = 1
    timeout: float = 4
    poll: float = 0.25
    verify_timeout: float = 15
    category: str | None = None

    def __post_init__(self):
        for key in ('baseline','exposure','recovery','rate','timeout','poll','verify_timeout'):
            value = getattr(self, key)
            if not isinstance(value, (int,float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{key} must be finite and positive')
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 64:
            raise ValueError('concurrency must be 1..64')
        if type(self.seed) is not int:
            raise ValueError('seed must be an integer')
        if self.category is not None and self.category not in CATEGORIES:
            raise ValueError('Unknown category; use exact proposal name')
        if self.timeout < 3 or self.verify_timeout < self.timeout:
            raise ValueError('timeout must be >=3 and verify_timeout >= timeout')

def workload(seed):
    rng = random.Random(seed)
    while True:
        choice = rng.randrange(3)
        if choice == 0:
            yield ('GET', '/products', None)
        elif choice == 1:
            yield ('GET', f'/products/{rng.randint(1,10)}/quote', None)
        else:
            yield ('POST', '/orders', {'product_id': rng.randint(1,10), 'quantity': rng.randint(1,5)})

def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00').replace(' UTC', '+00:00'))

def export(source: Path, destination: Path, start: str, end: str, db=False):
    """Half-open UTC boundary; retain original native JSON records, no deduplication."""
    count = 0
    with destination.open('w') as out:
        for path in sorted(source.glob('*.json') if db else [source]):
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                record = json.loads(line)  # malformed/partial records invalidate export
                time = record.get('timestamp')
                if time and timestamp(start) <= timestamp(time) < timestamp(end):
                    out.write(line + '\n')
                    count += 1
    return count

def validate_ground_truth(data):
    Config(**data['config'])
    if data['valid']:
        if data['invalidity_reasons'] or not data.get('recovery_verified_at'):
            raise ValueError('Valid run requires successful recovery')
        if data['config']['category'] and not data.get('first_observed_failure'):
            raise ValueError('Faulty run requires observed failure')
    if timestamp(data['end']) < timestamp(data['start']):
        raise ValueError('Inverted run interval')

def save(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')
