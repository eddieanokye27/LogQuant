import asyncio
import time
from unittest.mock import patch

import httpx
import pytest

from fixture.core import CATEGORIES, Config, workload
from fixture.runner import failure_probe, healthy_probe, traffic
from fixture.timing import TimingGuard


@pytest.mark.parametrize('category',[None,*CATEGORIES])
def test_probe_operation_mix_does_not_depend_on_category(category):
    seen=[]
    class Control:
        def state(self,category): return {'independent_proof':True}
    def respond(req):
        seen.append((req.method,req.url.path))
        # For permission verification, successful reads prove authentication.
        return httpx.Response(500 if req.method=='POST' else 200)
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond),base_url='http://app') as client:
            if category: await failure_probe(client,Control(),Config(category=category,poll=.001))
            else: await healthy_probe(client)
    asyncio.run(check())
    assert seen==[('GET','/ready'),('GET','/products'),('GET','/products/1/quote'),('POST','/orders')]


def test_permission_http_500_is_not_enough_if_authentication_also_fails():
    class Control:
        def state(self,category): return {'insert_denied':True}
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(500)),base_url='http://app') as client:
            row=await failure_probe(client,Control(),Config(category=CATEGORIES[3],poll=.001))
            assert not row['verified'] and not row['state']['authenticated']
    asyncio.run(check())


@pytest.mark.parametrize('mono_delta',[.1,86.3])
def test_pause_or_wall_clock_jump_is_rejected(mono_delta):
    with patch('fixture.timing.time.time',return_value=1000),patch('fixture.timing.time.monotonic',return_value=100):
        guard=TimingGuard()
    guard.tick(wall=1086.3,mono=100+mono_delta)
    with pytest.raises(RuntimeError,match='Timing acceptance'):guard.check()


def test_awaited_fault_response_delay_is_not_a_harness_pause():
    async def respond(req):
        await asyncio.sleep(.25)
        return httpx.Response(500)
    async def check():
        guard=TimingGuard();monitor=asyncio.create_task(guard.monitor())
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond),base_url='http://app') as client:
                results=[]
                await traffic(client,Config(rate=4),.3,workload(1),results,'exposure',guard)
            guard.tick();guard.check()
            assert results and max(r['latency_ms'] for r in results)>=240
            assert max(r['scheduler_delay_ms'] for r in results)<500
            assert all('queue_wait_ms' in r and 'started_at' in r and 'scheduled_at' in r for r in results)
        finally:
            monitor.cancel();await asyncio.gather(monitor,return_exceptions=True)
    asyncio.run(check())


def test_blocking_harness_pause_rejects_scheduled_traffic():
    first=True
    async def respond(req):
        nonlocal first
        if first:
            first=False
            time.sleep(.9)  # Deliberately block the harness; unlike an awaited fault response.
        return httpx.Response(200)
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond),base_url='http://app') as client:
            with pytest.raises(RuntimeError,match='scheduler_delay'):
                await traffic(client,Config(rate=4),1,workload(1),[],'baseline',TimingGuard())
    asyncio.run(check())


@pytest.mark.parametrize('category',[None,*CATEGORIES])
def test_phase_probe_deadlines_and_count_are_category_independent(category):
    from fixture.runner import run_phase
    seen=[]
    class Control:
        def state(self,category):return {'independent':True}
    def respond(req):
        seen.append(req.url.path)
        failed = category in CATEGORIES[:2] or (category==CATEGORIES[2] and req.url.path.endswith('/quote')) or (category in CATEGORIES[3:] and req.method=='POST')
        return httpx.Response(500 if failed else 200)
    async def no_traffic(*args):return 0
    async def check():
        data={'observations':[],'first_observed_failure':None,'last_observed_failure':None,'recovery_verified_at':None}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond),base_url='http://app') as client:
            with patch('fixture.runner.PROBE_INTERVAL',.02),patch('fixture.runner.traffic',no_traffic):
                await run_phase(client,Control(),Config(category=category,poll=.001),.05,workload(1),[],'exposure',data,TimingGuard())
        assert [r['scheduled_offset_seconds'] for r in data['observations']]==[0,.02,.04]
        assert len(seen)==12
    asyncio.run(check())
