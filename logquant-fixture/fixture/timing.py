"""Evaluation-only continuity guards; prospective workload distortion policy v3."""
import asyncio
from datetime import datetime, timezone
import time
from fixture.scheduling import POLICY, LEGACY_POLICY, WorkloadAccounting

LIMITS = {'heartbeat_gap_seconds': 1.0, 'wall_monotonic_divergence_seconds': 1.0,
          'scheduler_delay_seconds': 0.5, 'queue_wait_seconds': 1.0,
          'phase_overrun_seconds': 6.0}
TRACE_VERSION = 'continuity-trace-v5;bracketed;acceptance-v4-whole-run'


def clock_sample():
    before = time.monotonic()
    wall = time.time()
    after = time.monotonic()
    return {'wall':wall,'at':datetime.fromtimestamp(wall,timezone.utc).isoformat(),
            'mono_before':before,'mono_after':after,'width_seconds':after-before,
            'offset_lower':wall-after,'offset_upper':wall-before}


class TimingGuard:
    def __init__(self, rate=None, planned_workload=None, whole_run=False):
        self.whole_run = whole_run
        initial = clock_sample()
        self.workload = WorkloadAccounting(rate, planned_workload,
            initial['mono_after'] if whole_run else None) if rate is not None else None
        self.last_wall = initial['wall']
        self.last_mono = initial['mono_after']
        self.initial_offset = self.last_wall-self.last_mono
        self.last_cpu = time.process_time()
        self.recent = []
        self.summary = {'trace_version': TRACE_VERSION,
                        'acceptance_policy': POLICY if whole_run else LEGACY_POLICY,
                        'initial_clock_sample':initial,'clock_discontinuity_observed':False,
                        'max_clock_sample_width_seconds':initial['width_seconds'],
                        'clock_warnings':[],
                        'limits': LIMITS.copy(), 'heartbeat_samples': 0,
                        'max_heartbeat_gap_seconds': 0.0,
                        'max_wall_monotonic_divergence_seconds': 0.0,
                        'violations': [], 'phases': [], 'notable_heartbeats': []}

    def violation(self, kind, value, **context):
        target = self.summary['clock_warnings'] if self.whole_run and kind=='clock_divergence' else self.summary['violations']
        if len(target) < 20:
            target.append({'at': datetime.now(timezone.utc).isoformat(),
                                               'kind': kind, 'seconds': value, **context})

    def tick(self, wall=None, mono=None):
        bracket = clock_sample() if wall is None and mono is None else {
            'wall':wall,'at':datetime.fromtimestamp(wall,timezone.utc).isoformat(),
            'mono_before':mono,'mono_after':mono,'width_seconds':0,
            'offset_lower':wall-mono,'offset_upper':wall-mono}
        wall,mono = bracket['wall'],bracket['mono_after']
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
        bracket['signed_offset_change_seconds'] = wall-mono-self.initial_offset
        self.recent = (self.recent+[bracket])[-20:]
        self.summary['last_clock_sample'] = bracket
        self.summary['max_clock_sample_width_seconds'] = max(self.summary['max_clock_sample_width_seconds'],bracket['width_seconds'])
        if divergence>LIMITS['wall_monotonic_divergence_seconds']:
            self.summary['clock_discontinuity_observed'] = True
            if 'first_clock_change_context' not in self.summary:
                self.summary['first_clock_change_context'] = list(self.recent)
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
        errors = self.fatal_violations()
        if errors:
            raise RuntimeError('Timing acceptance failed: '+errors[0]['kind'])

    def fatal_violations(self):
        return [v for v in self.summary['violations'] if not (self.whole_run and v['kind']=='clock_divergence')]
