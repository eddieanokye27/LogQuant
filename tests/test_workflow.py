import asyncio
from copy import deepcopy
import json
from pathlib import Path
import runpy
from unittest.mock import patch

import httpx2
import pytest
from logquant.agents.contracts import ExecutionBudget
from logquant.agents.provider import MODEL, ConservativeCounter, OpenAIAdapter, Usage, strict_schema, wire_body
from logquant.agents.prompts import preflight
from logquant.agents.transport_mock import HTTPEvent, MockHTTP
from logquant.agents.validation import prepare_evidence
from logquant.agents.workflow import execute
from logquant.agents.run_cli import main
from logquant.evidence import EvidenceConfig, select_evidence
from logquant.evidence.cli import load_alert_export
from logquant.evidence.selection import payload_bytes
from logquant.ingestion import load_diagnostic_directory

ROOT = Path(__file__).resolve().parents[1]
CREATE = runpy.run_path(str(ROOT/'scripts/make_synthetic_evidence_example.py'))['create_example']
EXAMPLE = runpy.run_path(str(ROOT/'scripts/make_mock_agent_responses.py'))['example_responses']
KNOWN = Usage(input_tokens=100,output_tokens=50,total_tokens=150)


@pytest.fixture
def sample(tmp_path):
    source=CREATE(tmp_path/'synthetic')
    alerts, hashes, ah=load_alert_export(source.parent/'alerts')
    selection=select_evidence(load_diagnostic_directory(source),run_id=alerts.coverage.run_id,
        alerts=alerts,diagnostic_sha256=hashes,config=EvidenceConfig(max_records=9),alert_input_sha256=ah)
    context=prepare_evidence(selection.payloads[0],selection.report.bundles[0])
    triage,rca=EXAMPLE(context.payload)
    return context,triage,rca,selection


def events(triage,rca):
    return dict(triage=(HTTPEvent(output=json.dumps(triage),usage=KNOWN),),
                rca=(HTTPEvent(output=json.dumps(rca),usage=KNOWN),))


def run(context,values,budget=None):
    handler=MockHTTP(values)
    async def call():
        adapter=OpenAIAdapter(model=MODEL,api_key='mock-not-a-secret',transport=httpx2.MockTransport(handler))
        try:return await execute(context,budget or ExecutionBudget(),ConservativeCounter(MODEL),adapter,mode='mocked-transport')
        finally:await adapter.close()
    return asyncio.run(call()),handler


def test_real_graph_sdk_success_and_original_rca_evidence(sample):
    context,triage,rca,_=sample
    result,handler=run(context,events(triage,rca))
    assert result.status=='validated' and len(result.attempts)==len(handler.calls)==2
    assert result.triage is not None and result.rca.status=='insufficient_evidence'
    assert result.total_input_tokens==200 and result.total_output_tokens==100 and result.unknown_usage_attempts==0
    assert result.usage_is_mocked and result.estimated_cost_usd is None
    assert result.route_trace==('preflight','triage_prepare','triage_generate','triage_validate','rca_prepare','rca_generate','rca_validate','completion')
    data=json.loads(handler.calls[1]['body']['messages'][1]['content'])
    assert data['evidence']==context.payload.model_dump(mode='json')
    assert data['triage']==result.triage.model_dump(mode='json')
    assert all(a.outcome.request_id and a.client_request_id and a.schema_version and a.wire_schema_sha256 for a in result.attempts)
    assert all(a.outcome.latency_seconds>=0 for a in result.attempts)
    body=handler.calls[0]['body']
    assert body['max_completion_tokens']==1000 and body['reasoning_effort']=='none' and body['store'] is False
    assert body['response_format']['json_schema']['strict'] is True


def test_both_stage_repairs_four_calls_no_hidden_retry(sample):
    context,triage,rca,_=sample
    bad=deepcopy(triage);bad['symptoms'][0]['citations'][0]['line_id']='application.jsonl:999'
    values=events(triage,rca)
    values['triage']=(HTTPEvent(output=json.dumps(bad)),*values['triage'])
    values['rca']=(HTTPEvent(output='not JSON'),*values['rca'])
    result,handler=run(context,values)
    assert result.status=='validated' and len(result.attempts)==len(handler.calls)==result.max_generation_attempts==4
    assert result.attempts[0].validation_errors and result.attempts[2].validation_errors
    assert result.attempts[1].request.repair and result.attempts[3].request.repair
    assert 'triage_repair' in result.route_trace and 'rca_repair' in result.route_trace
    assert result.total_input_tokens is None and result.known_input_tokens==200 and result.unknown_usage_attempts==2


@pytest.mark.parametrize('status', ['refusal','truncated','rate_limited','provider_error','timeout','cancelled','malformed'])
def test_provider_terminal_outcomes_never_repair_or_invoke_rca(sample,status):
    context,triage,rca,_=sample
    values=events(triage,rca);values['triage']=(HTTPEvent(status=status,output='partial original content'),)
    result,handler=run(context,values)
    assert result.status in ('processing_failure','cancelled') and result.rca is None
    assert len(handler.calls)==len(result.attempts)==1
    expected='malformed_provider_response' if status=='malformed' else status
    assert result.attempts[0].outcome.status==expected
    assert result.total_input_tokens is None and result.unknown_usage_attempts==1
    assert not any(c['stage']=='rca' for c in handler.calls)


def test_citation_exhaustion_keeps_all_invalid_attempts(sample):
    context,triage,rca,_=sample
    bad=deepcopy(triage);bad['symptoms'][0]['citations'][0]['run_id']='another-run'
    values=dict(triage=(HTTPEvent(output=json.dumps(bad)),HTTPEvent(output=json.dumps(bad))),rca=())
    result,handler=run(context,values)
    assert result.status=='processing_failure' and len(result.attempts)==2
    assert result.triage is None and result.rca is None
    assert all(a.validation_errors and a.outcome.raw_output for a in result.attempts)
    result,handler=run(context,values,ExecutionBudget(max_repair_attempts=0))
    assert len(handler.calls)==1 and result.max_generation_attempts==2


def test_rca_failure_preserves_triage_and_refuses_fallback(sample):
    context,triage,rca,_=sample
    bad=deepcopy(rca);bad['observations'][0]['citations'][0]['line_id']='application.jsonl:1' # omitted
    values=events(triage,rca);values['rca']=(HTTPEvent(output=json.dumps(bad)),HTTPEvent(output=json.dumps(bad)))
    result,handler=run(context,values)
    assert result.status=='processing_failure' and result.triage is not None and result.rca is None
    assert len(handler.calls)==3 and result.attempts[-1].validation_errors


def test_actual_deadline_and_external_task_cancellation(sample):
    context,triage,rca,_=sample
    values=events(triage,rca);values['triage']=(HTTPEvent(output=json.dumps(triage),delay_seconds=.1),)
    result,handler=run(context,values,ExecutionBudget(timeout_seconds=.01))
    assert result.attempts[0].outcome.status=='timeout' and len(handler.calls)==1
    async def cancel():
        entered=asyncio.Event()
        async def transport(request):
            entered.set();await asyncio.sleep(10)
        adapter=OpenAIAdapter(model=MODEL,api_key='mock',transport=httpx2.MockTransport(transport))
        task=asyncio.create_task(execute(context,ExecutionBudget(),ConservativeCounter(MODEL),adapter,mode='mocked-transport'))
        await entered.wait();task.cancel()
        try:return await task
        finally:await adapter.close()
    result=asyncio.run(cancel())
    assert result.status=='cancelled' and len(result.attempts)==1
    assert result.attempts[0].outcome.status=='cancelled' and result.total_output_tokens is None


def test_preflight_and_repair_oversize_consume_no_extra_call(sample):
    context,triage,rca,_=sample
    result,handler=run(context,events(triage,rca),ExecutionBudget(max_input_tokens=1))
    assert result.status=='processing_failure' and not handler.calls and not result.attempts
    values=events(triage,rca);values['triage']=(HTTPEvent(output='word '*50000),)
    result,handler=run(context,values)
    assert len(handler.calls)==1 and len(result.attempts)==1 and 'cannot fit' in result.terminal_reason
    assert result.attempts[0].outcome.raw_output=='word '*50000


def test_schema_counter_cache_only_and_model_explicit(sample):
    context,_,_,_=sample
    with patch('requests.get',side_effect=AssertionError('No tokenizer/network fetch during runtime')):
        counter=ConservativeCounter(MODEL);request=preflight(context,ExecutionBudget(),counter)
    schema=strict_schema(request.body.response_schema)
    def check(node):
        if isinstance(node,dict):
            assert 'default' not in node
            assert 'minLength' not in node and 'maxLength' not in node and 'const' not in node
            if node.get('type')=='object':
                assert node['required']==list(node['properties']) and node['additionalProperties'] is False
            for value in node.values():check(value)
        elif isinstance(node,list):
            for value in node:check(value)
    check(schema)
    assert counter.encoding.name=='o200k_base' and counter.count_text('café')!=len('café'.encode())
    assert counter.count_request(request.body)>counter.count_text(json.dumps(wire_body(MODEL,request.body)))
    with pytest.raises(ValueError):ConservativeCounter('unspecified')
    with pytest.raises(ValueError):OpenAIAdapter(model='',api_key='mock')


def test_provider_error_cannot_echo_credentials_and_sdk_retries_disabled(sample):
    context,_,_,_=sample
    async def call():
        count=0
        def transport(request):
            nonlocal count;count+=1
            return httpx2.Response(401,json={'error':{'message':'SECRET_KEY_SHOULD_NOT_APPEAR','type':'auth','code':'invalid_api_key'}},headers={'x-request-id':'req-auth'})
        adapter=OpenAIAdapter(model=MODEL,api_key='SECRET_KEY_SHOULD_NOT_APPEAR',transport=httpx2.MockTransport(transport))
        assert adapter.client.max_retries==0
        try:result=await execute(context,ExecutionBudget(),ConservativeCounter(MODEL),adapter,mode='mocked-transport')
        finally:await adapter.close()
        assert count==1
        return result
    result=asyncio.run(call())
    assert 'SECRET_KEY_SHOULD_NOT_APPEAR' not in result.model_dump_json()
    assert result.attempts[0].outcome.request_id=='req-auth'


def test_estimate_underflow_and_provider_cap_violation_stop(sample):
    context,triage,rca,_=sample
    for usage,reason in [(Usage(input_tokens=999999,output_tokens=1,total_tokens=1000000),'estimate'),
                         (Usage(input_tokens=1,output_tokens=99999,total_tokens=100000),'cap violation')]:
        values=events(triage,rca);values['triage']=(HTTPEvent(output=json.dumps(triage),usage=usage),)
        result,handler=run(context,values)
        assert result.status=='processing_failure' and len(handler.calls)==1 and reason in result.terminal_reason


def test_cli_real_adapter_offline_live_guards_and_preservation(sample,tmp_path):
    context,triage,rca,selection=sample
    source=tmp_path/'evidence';source.mkdir()
    (source/'evidence.jsonl').write_bytes(payload_bytes(context.payload))
    (source/'evidence-report.json').write_text(selection.report.model_dump_json())
    fixture=tmp_path/'http.json'
    fixture.write_text(json.dumps(dict(schema_version=1,purpose='synthetic-test-only',candidates={context.payload.candidate_id:
        {stage:[event.model_dump(mode='json') for event in values] for stage,values in events(triage,rca).items()}})))
    snapshot=(source/'evidence.jsonl').read_bytes()
    assert main([str(source),'--mock-http',str(fixture),'--output-dir',str(tmp_path/'output')])==0
    value=json.loads((tmp_path/'output/workflow.json').read_text())
    assert value['mode']=='mocked-transport' and value['usage_is_mocked']
    with patch('logquant.agents.run_cli.OpenAIAdapter',side_effect=AssertionError('No live client allowed')):
        assert main([str(source),'--live','--output-dir',str(tmp_path/'bad')])==1
        assert main([str(source),'--output-dir',str(tmp_path/'bad')])==1
    assert (source/'evidence.jsonl').read_bytes()==snapshot


def test_graph_disables_environment_enabled_remote_tracing(sample):
    import os
    context,triage,rca,_=sample
    with patch.dict(os.environ,{'LANGSMITH_TRACING':'true','LANGCHAIN_TRACING_V2':'true','LANGSMITH_API_KEY':'mock'}), \
         patch('langsmith.Client.create_run',side_effect=AssertionError('Remote tracing must stay disabled')):
        result,handler=run(context,events(triage,rca))
    assert result.status=='validated' and len(handler.calls)==2
