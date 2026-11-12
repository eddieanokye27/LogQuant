from unittest.mock import patch
import pytest
from fixture.timing import TimingGuard,clock_sample
from fixture.scheduling import WorkloadAccounting,utc,POLICY


def test_bracket_sample_records_uncertainty():
    with patch('fixture.timing.time.monotonic',side_effect=[10,10.25]),patch('fixture.timing.time.time',return_value=100):
        s=clock_sample()
    assert s['offset_lower']==89.75 and s['offset_upper']==90 and s['width_seconds']==.25


def test_clock_reversal_flag_does_not_waive_heartbeat_or_queue_guard():
    with patch('fixture.timing.time.monotonic',return_value=0),patch('fixture.timing.time.time',return_value=100):
        guard=TimingGuard(4,1200,whole_run=True)
    for i in range(1,41):guard.tick(wall=98+i/10,mono=i/10)
    guard.check()
    assert guard.summary['clock_discontinuity_observed'] and len(guard.summary['clock_warnings'])==20
    assert guard.summary['first_clock_change_context'] and not guard.summary['violations']
    guard.violation('queue_wait',1.01)
    with pytest.raises(RuntimeError,match='queue_wait'):guard.check()


def test_monotonic_distortion_does_not_charge_utc_step():
    a=WorkloadAccounting(4,1200,monotonic_origin=100)
    a.start_phase('baseline',2,100,29.9)
    a.record('baseline',0,('GET','/products',None),100,utc(29.9),100.01,utc(1000),'emitted')
    assert a.snapshot(1)['unique_distorted_slots']==0
    assert a.snapshot(1)['policy']==POLICY
    a.record('baseline',1,('GET','/products',None),129.99,utc(10),130.01,utc(10),'emitted')
    s=a.snapshot(2)
    assert s['unique_distorted_slots']==1
    assert [b['distorted_count'] for b in s['buckets']]==[1,1]
