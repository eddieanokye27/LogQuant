"""Prospective v3 workload scheduling/accounting. Evaluation data only."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import math

LEGACY_POLICY = 'acceptance-v3-distortion'
POLICY = 'acceptance-v4-whole-run'
DISTORTION = {'window_seconds': 30, 'bucket_percent': 5, 'run_percent': 1,
              'maximum_lateness_periods': 0.5, 'minimum_spacing_periods': 0.5}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def bucket(value: str) -> str:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Scheduling timestamps require a timezone')
    index = (stamp.astimezone(timezone.utc)-EPOCH)//timedelta(seconds=30)
    return (EPOCH+timedelta(seconds=index*30)).isoformat()


class WorkloadAccounting:
    """A finite per-run ledger; every processed slot consumes one seeded operation."""
    def __init__(self, rate: float, planned: int, monotonic_origin: float | None = None):
        if not math.isfinite(rate) or rate <= 0 or type(planned) is not int or planned <= 0:
            raise ValueError('Positive finite rate and positive planned count required')
        self.rate, self.planned = rate, planned
        self.period = 1/rate
        self.monotonic_origin = monotonic_origin
        self.bucket_limit = int(Fraction(str(rate))*30*5/100)
        self.run_limit = planned//100
        self.phases: list[dict] = []
        self.arrivals: list[dict] = []
        self.buckets: dict[str, Counter] = {}
        self.counts: Counter = Counter()
        self.last_emitted: float | None = None
        self.resume_after = -math.inf

    def _bucket(self, name: str) -> Counter:
        return self.buckets.setdefault(name, Counter())

    def _bin(self, mono: float, wall: str):
        return math.floor((mono-self.monotonic_origin)/30) if self.monotonic_origin is not None else bucket(wall)

    def start_phase(self, phase: str, count: int, mono: float, wall: float) -> None:
        if any(p['phase'] == phase for p in self.phases):
            raise ValueError('Duplicate scheduling phase')
        self.phases.append({'phase': phase, 'planned_slots': count,
                            'start_monotonic': mono, 'origin_wall_seconds': wall})
        for i in range(count):
            self._bucket(self._bin(mono+i/self.rate,utc(wall+i/self.rate)))['planned'] += 1

    def skip_reason(self, due: float, observed: float) -> str | None:
        if due <= self.resume_after:
            return 'expired_backlog'
        if observed-due > self.period/2:
            self.resume_after = observed
            return 'late_wake'
        if self.last_emitted is not None and observed-self.last_emitted < self.period/2:
            self.resume_after = observed
            return 'minimum_spacing'
        return None

    def record(self, phase: str, index: int, operation: tuple, due: float,
               scheduled_at: str, observed: float, observed_at: str,
               outcome: str, reason: str | None = None) -> None:
        if outcome not in ('emitted', 'scheduler_skip', 'queue_full'):
            raise ValueError('Unknown scheduled-arrival outcome')
        row = {'phase': phase, 'sequence': index, 'operation': list(operation),
               'planned_monotonic': due, 'scheduled_at': scheduled_at,
               'observed_monotonic': observed, 'observed_at': observed_at,
               'scheduler_delay_ms': max(0, observed-due)*1000,
               'outcome': outcome, 'skip_reason': reason}
        self.arrivals.append(row)
        self.counts[outcome] += 1
        if row['scheduler_delay_ms'] > 500:
            self.counts['warnings_over_500ms'] += 1
        origin = self._bin(due,scheduled_at)
        if outcome == 'scheduler_skip':
            self._bucket(origin)['scheduler_skips'] += 1
            self._bucket(origin)['own_distorted'] += 1
            self.counts['distorted'] += 1
        elif outcome == 'queue_full':
            self._bucket(origin)['queue_full'] += 1
        else:
            self.last_emitted = observed
            destination = self._bin(observed,observed_at)
            self._bucket(destination)['emitted'] += 1
            if destination != origin:
                self._bucket(origin)['own_distorted'] += 1
                self._bucket(origin)['moved_out'] += 1
                self._bucket(destination)['moved_in'] += 1
                self.counts['distorted'] += 1

    def violations(self) -> list[dict]:
        errors = []
        if self.counts['distorted'] > self.run_limit:
            errors.append({'kind': 'workload_distortion', 'scope': 'run',
                           'count': self.counts['distorted'], 'allowed': self.run_limit})
        for name, values in sorted(self.buckets.items()):
            count = values['own_distorted']+values['moved_in']
            if count > self.bucket_limit:
                errors.append({'kind': 'workload_distortion', 'scope': 'bucket',
                               'bucket_start': name, 'count': count, 'allowed': self.bucket_limit})
        return errors

    def snapshot(self, completed: int) -> dict:
        phase_counts: dict[str, Counter] = {}
        for row in self.arrivals:
            counts = phase_counts.setdefault(row['phase'], Counter())
            counts['processed'] += 1
            counts[row['outcome']] += 1
        phases = []
        for phase in self.phases:
            counts = phase_counts.get(phase['phase'], Counter())
            phases.append({**phase, 'processed_slots': counts['processed'],
                           'unreached_slots': phase['planned_slots']-counts['processed'],
                           'emitted_slots': counts['emitted'],
                           'scheduler_skips': counts['scheduler_skip'],
                           'queue_full_drops': counts['queue_full']})
        buckets = []
        for name, c in sorted(self.buckets.items()):
            values = {k: c[k] for k in ('planned', 'emitted', 'scheduler_skips',
                                       'queue_full', 'own_distorted', 'moved_out', 'moved_in')}
            buckets.append({'start': name, **values,
                            'distorted_count': c['own_distorted']+c['moved_in']})
        result = {'policy': POLICY if self.monotonic_origin is not None else LEGACY_POLICY,
                'distortion_limits': DISTORTION.copy(),
                'rate': self.rate, 'planned_slots': self.planned,
                'bucket_allowed_slots': self.bucket_limit, 'run_allowed_slots': self.run_limit,
                'processed_slots': len(self.arrivals), 'unreached_slots': self.planned-len(self.arrivals),
                'emitted_slots': self.counts['emitted'], 'scheduler_skips': self.counts['scheduler_skip'],
                'queue_full_drops': self.counts['queue_full'], 'unique_distorted_slots': self.counts['distorted'],
                'warnings_over_500ms': self.counts['warnings_over_500ms'], 'completed_requests': completed,
                'max_scheduler_delay_ms': max((r['scheduler_delay_ms'] for r in self.arrivals),default=None),
                'accounting_complete': len(self.arrivals) == self.planned and completed == self.counts['emitted'],
                'phases': phases, 'buckets': buckets, 'arrivals': self.arrivals}
        if self.monotonic_origin is not None:
            result.update(monotonic_origin=self.monotonic_origin,distortion_basis='run-monotonic-30s')
        return result


def verify_accounting(ground: dict, requests: list[dict]) -> dict:
    """Recompute a complete v3 ledger, seeded operations, spacing and row linkage."""
    from fixture.core import workload
    saved = ground['timing']['workload']
    cfg = ground['config']
    phases = ('baseline', 'exposure', 'recovery')
    planned = sum(math.ceil(cfg[p]*cfg['rate']) for p in phases)
    if saved['policy'] not in (POLICY,LEGACY_POLICY):raise ValueError('Unknown workload policy')
    check = WorkloadAccounting(cfg['rate'], planned,
        saved['monotonic_origin'] if saved['policy']==POLICY else None)
    sequence = workload(cfg['seed'])
    if [p['phase'] for p in saved['phases']] != list(phases):
        raise ValueError('Incomplete scheduling phases')
    cursor = 0
    emitted = {}
    previous = -math.inf
    for phase in saved['phases']:
        name, count = phase['phase'], math.ceil(cfg[phase['phase']]*cfg['rate'])
        if phase['planned_slots'] != count:
            raise ValueError('Scheduling phase plan differs from configuration')
        start, wall = phase['start_monotonic'], phase['origin_wall_seconds']
        check.start_phase(name, count, start, wall)
        for i in range(count):
            if cursor >= len(saved['arrivals']):
                raise ValueError('Incomplete scheduled-arrival ledger')
            row = saved['arrivals'][cursor];cursor += 1
            operation = next(sequence)
            due = start+i/cfg['rate']
            observed = row['observed_monotonic']
            if (row['phase'] != name or row['sequence'] != i or row['operation'] != list(operation)
                    or row['planned_monotonic'] != due or row['scheduled_at'] != utc(wall+i/cfg['rate'])
                    or not math.isfinite(observed) or observed < due or observed < previous):
                raise ValueError('Scheduled-arrival identity/seed/deadline/order mismatch')
            previous = observed
            wall_stamp = datetime.fromisoformat(row['observed_at'])
            if wall_stamp.tzinfo is None or (saved['policy']==LEGACY_POLICY and abs(wall_stamp.timestamp()-(wall+observed-start)) > 1.001):
                raise ValueError('Enqueue UTC is incompatible with monotonic/phase clocks')
            reason = check.skip_reason(due, observed)
            if row['outcome'] != ('scheduler_skip' if reason else 'emitted') or row['skip_reason'] != reason:
                raise ValueError('Scheduler no-backlog/spacing decision differs from ledger')
            check.record(name, i, operation, due, row['scheduled_at'], observed,
                         row['observed_at'], row['outcome'], reason)
            if row['outcome'] == 'emitted':
                emitted[name, i] = row
    if cursor != len(saved['arrivals']) or check.snapshot(len(requests)) != saved:
        raise ValueError('Workload distortion summary differs from recomputed ledger')
    if check.violations() or not saved['accounting_complete'] or saved['queue_full_drops']:
        raise ValueError('Workload distortion budget/completeness failed')
    seen = set()
    for row in requests:
        key = row['phase'], row['sequence']
        if key in seen or key not in emitted:
            raise ValueError('Duplicate/foreign completed workload identity')
        seen.add(key);arrival = emitted[key]
        if (row['scheduled_at'] != arrival['scheduled_at']
                or row['planned_monotonic'] != arrival['planned_monotonic']
                or row['enqueued_at'] != arrival['observed_at']
                or row['enqueued_monotonic'] != arrival['observed_monotonic']
                or row['scheduler_delay_ms'] != arrival['scheduler_delay_ms']
                or (row['method'], row['path']) != tuple(arrival['operation'][:2])
                or not row['enqueued_monotonic'] <= row['dispatch_monotonic'] <= row['request_started_monotonic'] <= row['request_finished_monotonic']
                or row['queue_wait_ms'] > 1000):
            raise ValueError('Completed workload evidence differs from emitted arrival')
    if seen != set(emitted):
        raise ValueError('Missing completed emitted workload')
    return {k: saved[k] for k in ('planned_slots', 'emitted_slots', 'scheduler_skips',
                                 'unique_distorted_slots', 'bucket_allowed_slots', 'run_allowed_slots')}
