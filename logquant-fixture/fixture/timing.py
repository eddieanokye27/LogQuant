"""Evaluation-only continuity checks, fixed before readiness validation v2."""
import asyncio
from datetime import datetime, timezone
import time

LIMITS = {'heartbeat_gap_seconds': 1.0, 'wall_monotonic_divergence_seconds': 1.0,
          'scheduler_delay_seconds': 0.5, 'queue_wait_seconds': 1.0,
          'phase_overrun_seconds': 6.0}


class TimingGuard:
    def __init__(self):
        self.last_wall = time.time()
        self.last_mono = time.monotonic()
        self.initial_offset = self.last_wall-self.last_mono
        self.summary = {'limits': LIMITS.copy(), 'heartbeat_samples': 0,
                        'max_heartbeat_gap_seconds': 0.0,
                        'max_wall_monotonic_divergence_seconds': 0.0,
                        'violations': [], 'phases': []}

    def violation(self, kind, value):
        if len(self.summary['violations']) < 20:
            self.summary['violations'].append({'at': datetime.now(timezone.utc).isoformat(),
                                               'kind': kind, 'seconds': value})

    def tick(self, wall=None, mono=None):
        wall = time.time() if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        gap = mono-self.last_mono
        divergence = abs((wall-mono)-self.initial_offset)
        self.summary['heartbeat_samples'] += 1
        self.summary['max_heartbeat_gap_seconds'] = max(self.summary['max_heartbeat_gap_seconds'],gap)
        self.summary['max_wall_monotonic_divergence_seconds'] = max(
            self.summary['max_wall_monotonic_divergence_seconds'],divergence)
        if gap > LIMITS['heartbeat_gap_seconds']: self.violation('heartbeat_gap',gap)
        if divergence > LIMITS['wall_monotonic_divergence_seconds']: self.violation('clock_divergence',divergence)
        self.last_wall, self.last_mono = wall, mono

    async def monitor(self):
        while True:
            await asyncio.sleep(.1)
            self.tick()

    def check(self):
        if self.summary['violations']:
            raise RuntimeError('Timing acceptance failed: '+self.summary['violations'][0]['kind'])
