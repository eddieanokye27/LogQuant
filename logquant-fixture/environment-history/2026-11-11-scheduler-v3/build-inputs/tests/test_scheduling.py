"""Synthetic clocks/operations only; no research or live fixture collection."""
import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest

from fixture.core import Config, workload
from fixture.runner import traffic
from fixture.scheduling import WorkloadAccounting, bucket, utc, verify_accounting
from fixture.timing import LIMITS, TimingGuard


@pytest.mark.parametrize('rate,bucket_limit,run_limit',[(4,6,12),(5,7,15),(6,9,18)])
def test_exact_fraction_budgets_and_integer_floor(rate,bucket_limit,run_limit):
    accounting=WorkloadAccounting(rate,rate*300)
    assert accounting.bucket_limit==bucket_limit and accounting.run_limit==run_limit
    accounting.counts['distorted']=run_limit
    accounting._bucket(utc(0))['own_distorted']=bucket_limit
    assert accounting.violations()==[]
    accounting.counts['distorted']+=1
    accounting._bucket(utc(0))['moved_in']=1
    assert {v['scope'] for v in accounting.violations()}=={'run','bucket'}


def test_half_period_lateness_spacing_and_future_deadline_boundaries():
    a=WorkloadAccounting(4,1200)
    assert a.skip_reason(100,100.125) is None  # Exactly half a period is allowed.
    assert a.skip_reason(100,100.125001)=='late_wake'
    assert a.skip_reason(100.125001,100.125001)=='expired_backlog'  # Strictly future after skip.
    assert a.skip_reason(100.25,100.25) is None
    a.last_emitted=100.25
    assert a.skip_reason(100.375,100.375) is None  # Exactly half-period spacing.
    assert a.skip_reason(100.374999,100.374999)=='minimum_spacing'


def test_migration_counts_both_buckets_once_in_run_and_preserves_duplicates():
    a=WorkloadAccounting(4,1200)
    for i in range(2):
        a.record('baseline',i,('GET','/products',None),10+i*.25,utc(29.999999),
                 10.01+i*.25,utc(30),'emitted')
    s=a.snapshot(2)
    assert bucket(utc(29.999999))==utc(0)
    assert bucket(utc(30))==utc(30)
    assert s['unique_distorted_slots']==2
    assert [b['distorted_count'] for b in s['buckets']]==[2,2]
    assert s['buckets'][0]['own_distorted']==2 and s['buckets'][1]['moved_in']==2
    assert len(s['arrivals'])==2  # Repeated ordinary operations are separate planned slots.
    assert not s['accounting_complete'] and s['unreached_slots']==1198


def test_skip_is_distorted_once_queue_full_is_separate_and_warning_is_not_fatal():
    a=WorkloadAccounting(4,1200)
    a.record('baseline',0,('GET','/products',None),1,utc(0),1.7,utc(.7),'scheduler_skip','late_wake')
    a.record('baseline',1,('GET','/products',None),2,utc(1),2,utc(1),'queue_full')
    s=a.snapshot(0)
    assert s['warnings_over_500ms']==1 and s['unique_distorted_slots']==1
    assert s['scheduler_skips']==1 and s['queue_full_drops']==1 and a.violations()==[]
    assert not s['accounting_complete']


def test_no_backlog_replay_seed_mapping_across_phases_and_minimum_spacing():
    class Clock:
        value=100.0
        injected=False
        def monotonic(self):return self.value
        def time(self):return 1760000000+self.value-100
    clock=Clock();real_sleep=asyncio.sleep
    async def sleep(delay):
        clock.value+=max(0,delay)
        if not clock.injected and clock.value==100.25:
            clock.value+=.7;clock.injected=True
        await real_sleep(0)
    seen=[]
    async def request(client,operation,timeout):
        seen.append(operation)
        return {'status':200,'method':operation[0],'path':operation[1],
                'started_at':utc(clock.time()),'at':utc(clock.time()),'latency_ms':0,
                'request_started_monotonic':clock.value,'request_finished_monotonic':clock.value}
    async def check():
        guard=TimingGuard(4,1200);rows=[];stream=workload(10004)
        with patch('fixture.runner.time',clock),patch('fixture.runner.now',lambda:utc(clock.time())), \
             patch('fixture.runner.asyncio.sleep',sleep),patch('fixture.runner.request',request):
            await traffic(None,Config(rate=4),2,stream,rows,'baseline',guard,100,1760000000)
            await traffic(None,Config(rate=4),2,stream,rows,'exposure',guard,102,1760000002)
        arrivals=guard.workload.arrivals
        assert len(arrivals)==16
        skipped=[r for r in arrivals if r['outcome']=='scheduler_skip']
        assert [r['sequence'] for r in skipped]==[1,2,3]
        assert [r['skip_reason'] for r in skipped]==['late_wake','expired_backlog','expired_backlog']
        source=workload(10004);expected=[next(source) for _ in range(16)]
        assert [r['operation'] for r in arrivals]==[list(o) for o in expected]
        assert seen==[o for i,o in enumerate(expected) if i not in (1,2,3)]
        emitted=[r['observed_monotonic'] for r in arrivals if r['outcome']=='emitted']
        assert all(b-a>=.125 for a,b in zip(emitted,emitted[1:]))
        assert guard.workload.snapshot(len(rows))['warnings_over_500ms']==1
        assert not guard.summary['violations']
    asyncio.run(check())


def complete_example():
    cfg=Config(baseline=10,exposure=10,recovery=10,rate=4)
    a=WorkloadAccounting(4,120);stream=workload(cfg.seed);rows=[]
    for j,phase in enumerate(('baseline','exposure','recovery')):
        a.start_phase(phase,40,j*10,1760000000+j*10)
        for i in range(40):
            due=j*10+i/4;stamp=utc(1760000000+due);op=next(stream)
            a.record(phase,i,op,due,stamp,due,stamp,'emitted')
            rows.append({'phase':phase,'sequence':i,'method':op[0],'path':op[1],
                         'scheduled_at':stamp,'planned_monotonic':due,'scheduler_delay_ms':0,
                         'enqueued_at':stamp,'enqueued_monotonic':due,'dispatch_monotonic':due,
                         'request_started_monotonic':due,'request_finished_monotonic':due+.001,
                         'queue_wait_ms':0})
    return {'config':cfg.__dict__,'timing':{'workload':a.snapshot(len(rows))}},rows


def test_full_ledger_verification_and_deterministic_snapshot():
    ground,rows=complete_example()
    assert verify_accounting(ground,list(reversed(rows)))['planned_slots']==120
    assert complete_example()==(ground,rows)


@pytest.mark.parametrize('damage',['seed','summary','missing_slot','duplicate_row','missing_completion',
                                 'deadline','spacing','bucket','policy','limits','queue','phase'])
def test_recomputed_ledger_rejects_tampering_and_incompleteness(damage):
    ground,rows=complete_example();w=ground['timing']['workload'];r=w['arrivals'][0]
    if damage=='seed':r['operation'][1]='/invented'
    elif damage=='summary':w['unique_distorted_slots']=1
    elif damage=='missing_slot':w['arrivals'].pop()
    elif damage=='duplicate_row':rows[-1]=deepcopy(rows[0])
    elif damage=='missing_completion':rows.pop()
    elif damage=='deadline':r['planned_monotonic']+=.01
    elif damage=='spacing':w['arrivals'][1]['observed_monotonic']=.01
    elif damage=='bucket':r['observed_at']=utc(1760000100)
    elif damage=='policy':w['policy']='arbitrary'
    elif damage=='limits':w['distortion_limits']['run_percent']=99
    elif damage=='queue':rows[0]['queue_wait_ms']=1001
    elif damage=='phase':w['phases'].pop()
    with pytest.raises(ValueError):verify_accounting(ground,rows)


def test_partial_cancellation_does_not_become_complete():
    async def check():
        guard=TimingGuard(4,1200);rows=[]
        async def request(*args):await asyncio.Event().wait()
        with patch('fixture.runner.request',request):
            task=asyncio.create_task(traffic(None,Config(rate=4),300,workload(1),rows,'baseline',guard))
            await asyncio.sleep(.01);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        snapshot=guard.workload.snapshot(len(rows))
        assert snapshot['emitted_slots']==1 and snapshot['completed_requests']==0
        assert snapshot['unreached_slots']==1199 and not snapshot['accounting_complete']
    asyncio.run(check())


def test_other_timing_limits_are_unchanged():
    assert LIMITS=={'heartbeat_gap_seconds':1.0,'wall_monotonic_divergence_seconds':1.0,
                    'scheduler_delay_seconds':.5,'queue_wait_seconds':1.0,'phase_overrun_seconds':6.0}


def test_real_slot_records_trigger_independent_bucket_and_run_budgets():
    for offsets,expected_scope in [(list(range(7)),'bucket'),([i*30+j for i in range(3) for j in range(5)],'run')]:
        a=WorkloadAccounting(4,1200)
        for i,offset in enumerate(offsets):
            a.record('baseline',i,('GET','/products',None),offset,utc(offset),
                     offset+.2,utc(offset+.2),'scheduler_skip','late_wake')
        assert expected_scope in {v['scope'] for v in a.violations()}


def test_queue_full_remains_a_fatal_overload_and_is_retained():
    async def check():
        guard=TimingGuard(100,30000);rows=[]
        async def never(*args):await asyncio.Event().wait()
        with patch('fixture.runner.request',never):
            with pytest.raises(RuntimeError,match='queue_full'):
                await traffic(None,Config(rate=100,concurrency=1),.1,workload(1),rows,'baseline',guard)
        assert guard.workload.snapshot(len(rows))['queue_full_drops']==1
        assert not rows and not guard.workload.snapshot(0)['accounting_complete']
    asyncio.run(check())


def load_audit():
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('fixture_export_audit',Path('scripts/verify_validation.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_audit_explicit_v2_v3_paths_and_no_legacy_cutoff_bypass():
    audit=load_audit();ground,rows=complete_example()
    timing=ground['timing']
    timing.update(limits=LIMITS.copy(),violations=[],max_heartbeat_gap_seconds=.1,
                  max_wall_monotonic_divergence_seconds=0,acceptance_policy='acceptance-v3-distortion')
    assert audit.review_timing(ground,rows)=='acceptance-v3-distortion'
    timing['workload']['scheduler_skips']=1  # Recomputed, not trusted summary flags.
    with pytest.raises(ValueError,match='summary'):audit.review_timing(ground,rows)
    timing.pop('workload');timing.pop('acceptance_policy')
    assert audit.review_timing(ground,rows)=='acceptance-v2'
    rows[0]['scheduler_delay_ms']=501
    with pytest.raises(ValueError,match='V2 workload'):audit.review_timing(ground,rows)
    timing['acceptance_policy']='unknown'
    with pytest.raises(ValueError,match='Unknown'):audit.review_timing(ground,rows)


@pytest.mark.parametrize('guard',['heartbeat','clock','queue','changed_limit','violation'])
def test_v3_audit_does_not_relax_independent_guards(guard):
    audit=load_audit();ground,rows=complete_example();t=ground['timing']
    t.update(limits=LIMITS.copy(),violations=[],max_heartbeat_gap_seconds=.1,
             max_wall_monotonic_divergence_seconds=0,acceptance_policy='acceptance-v3-distortion')
    if guard=='heartbeat':t['max_heartbeat_gap_seconds']=1.001
    elif guard=='clock':t['max_wall_monotonic_divergence_seconds']=1.001
    elif guard=='queue':rows[0]['queue_wait_ms']=1001
    elif guard=='changed_limit':t['limits']['scheduler_delay_seconds']=1
    else:t['violations']=[{'kind':'probe_scheduler_delay','seconds':.501}]
    with pytest.raises(ValueError):audit.review_timing(ground,rows)


def test_complete_v3_accounting_can_accept_isolated_warning_without_replaying():
    cfg=Config(baseline=100,exposure=100,recovery=100,rate=4)
    a=WorkloadAccounting(4,1200);stream=workload(cfg.seed);rows=[];observed=0
    for j,phase in enumerate(('baseline','exposure','recovery')):
        a.start_phase(phase,400,j*100,1760000000+j*100)
        for i in range(400):
            due=j*100+i/4;observed=max(observed,due)
            if j==0 and i==1:observed+=.7
            scheduled=utc(1760000000+due);stamp=utc(1760000000+observed);op=next(stream)
            reason=a.skip_reason(due,observed)
            a.record(phase,i,op,due,scheduled,observed,stamp,'scheduler_skip' if reason else 'emitted',reason)
            if not reason:
                rows.append({'phase':phase,'sequence':i,'method':op[0],'path':op[1],
                             'scheduled_at':scheduled,'planned_monotonic':due,
                             'scheduler_delay_ms':(observed-due)*1000,
                             'enqueued_at':stamp,'enqueued_monotonic':observed,
                             'dispatch_monotonic':observed,'request_started_monotonic':observed,
                             'request_finished_monotonic':observed+.001,'queue_wait_ms':0})
    ground={'config':cfg.__dict__,'timing':{'workload':a.snapshot(len(rows)),
        'limits':LIMITS.copy(),'violations':[],'max_heartbeat_gap_seconds':.8,
        'max_wall_monotonic_divergence_seconds':0,'acceptance_policy':'acceptance-v3-distortion'}}
    assert load_audit().review_timing(ground,rows)=='acceptance-v3-distortion'
    snapshot=ground['timing']['workload']
    assert snapshot['scheduler_skips']==3 and snapshot['emitted_slots']==1197
    assert snapshot['warnings_over_500ms']==1 and snapshot['accounting_complete']
