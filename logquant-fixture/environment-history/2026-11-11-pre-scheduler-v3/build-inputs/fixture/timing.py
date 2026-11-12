"""Evaluation-only continuity checks, fixed before readiness validation v2."""
import asyncio
from datetime import datetime, timezone
import time

LIMITS = {'heartbeat_gap_seconds': 1.0, 'wall_monotonic_divergence_seconds': 1.0,
          'scheduler_delay_seconds': 0.5, 'queue_wait_seconds': 1.0,
          'phase_overrun_seconds': 6.0}
TRACE_VERSION = 'continuity-trace-v3;acceptance-v2-unchanged'


class TimingGuard:
    def __init__(self):
        self.last_wall = time.time()
        self.last_mono = time.monotonic()
        self.initial_offset = self.last_wall-self.last_mono
        self.last_cpu = time.process_time()
        self.summary = {'trace_version': TRACE_VERSION, 'limits': LIMITS.copy(), 'heartbeat_samples': 0,
                        'max_heartbeat_gap_seconds': 0.0,
                        'max_wall_monotonic_divergence_seconds': 0.0,
                        'violations': [], 'phases': [], 'notable_heartbeats': []}

    def violation(self, kind, value, **context):
        if len(self.summary['violations']) < 20:
            self.summary['violations'].append({'at': datetime.now(timezone.utc).isoformat(),
                                               'kind': kind, 'seconds': value, **context})

    def tick(self, wall=None, mono=None):
        wall = time.time() if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        gap = mono-self.last_mono
        cpu = time.process_time()
        sample = {'at': datetime.fromtimestamp(wall, timezone.utc).isoformat(),
                  'monotonic': mono, 'gap_seconds': gap,
                  'process_cpu_seconds': max(0, cpu-self.last_cpu)}
        if gap >= self.summary['max_heartbeat_gap_seconds']:
            self.summary['max_heartbeat_sample'] = sample
        if gap >= .25 and len(self.summary['notable_heartbeats']) < 32:
            self.summary['notable_heartbeats'].append(sample)
        divergence = abs((wall-mono)-self.initial_offset)
        self.summary['heartbeat_samples'] += 1
        self.summary['max_heartbeat_gap_seconds'] = max(self.summary['max_heartbeat_gap_seconds'],gap)
        self.summary['max_wall_monotonic_divergence_seconds'] = max(
            self.summary['max_wall_monotonic_divergence_seconds'],divergence)
        if gap > LIMITS['heartbeat_gap_seconds']: self.violation('heartbeat_gap',gap)
        if divergence > LIMITS['wall_monotonic_divergence_seconds']: self.violation('clock_divergence',divergence)
        self.last_wall, self.last_mono = wall, mono
        self.last_cpu = cpu

    async def monitor(self):
        while True:
            await asyncio.sleep(.1)
            self.tick()

    def check(self):
        if self.summary['violations']:
            raise RuntimeError('Timing acceptance failed: '+self.summary['violations'][0]['kind'])
