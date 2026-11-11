"""Mock HTTP responses beneath the real SDK adapter; never opens a socket."""
import asyncio
import json
from typing import Literal
import httpx2
from pydantic import Field
from logquant.ingestion.models import Contract
from .provider import MODEL, Usage


class HTTPEvent(Contract):
    status: Literal["completed","refusal","truncated","rate_limited","provider_error","timeout","cancelled","malformed"] = "completed"
    output: str = ""
    delay_seconds: float = Field(default=0.0,ge=0,le=1)
    usage: Usage | None = None


class HTTPFixture(Contract):
    schema_version: Literal[1]
    purpose: Literal["synthetic-test-only"]
    candidates: dict[str,dict[Literal["triage","rca"],tuple[HTTPEvent,...]]]


class MockHTTP:
    def __init__(self, events: dict[str,tuple[HTTPEvent,...]]):
        self.events = events
        self.calls: list[dict] = []

    async def __call__(self, request):
        body = json.loads(request.content)
        stage = "triage" if body["response_format"]["json_schema"]["schema"]["title"] == "TriageResponse" else "rca"
        index = sum(call["stage"] == stage for call in self.calls)
        self.calls.append({"stage":stage,"body":body})  # Deliberately never record HTTP headers/credentials.
        if index >= len(self.events.get(stage,())): raise ValueError("Mock transport events exhausted")
        event = self.events[stage][index]
        if event.delay_seconds: await asyncio.sleep(event.delay_seconds)
        if event.status == "timeout": raise httpx2.ReadTimeout("mock timeout",request=request)
        if event.status == "cancelled": raise asyncio.CancelledError()
        if event.status in ("rate_limited","provider_error"):
            return httpx2.Response(429 if event.status == "rate_limited" else 500,
                json={"error":{"message":"mock provider error","type":"mock","code":"mock"}},headers={"x-request-id":f"mock-error-{len(self.calls)}"})
        if event.status == "malformed": return httpx2.Response(200,json={"not":"a completion"})
        result = dict(id=f"mock-completion-{len(self.calls)}",object="chat.completion",created=0,model=MODEL,
            choices=[dict(index=0,finish_reason="length" if event.status == "truncated" else "stop",
                message=dict(role="assistant",content=event.output,refusal="Mock refusal" if event.status == "refusal" else None))])
        if event.usage is not None:
            result["usage"] = dict(prompt_tokens=event.usage.input_tokens,completion_tokens=event.usage.output_tokens,
                total_tokens=event.usage.total_tokens)
        return httpx2.Response(200,json=result,headers={"x-request-id":f"mock-request-{len(self.calls)}"})
