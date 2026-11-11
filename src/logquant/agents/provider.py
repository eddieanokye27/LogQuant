"""One text-only OpenAI adapter. No retries, tool execution or implicit model choice."""
import asyncio
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Literal

import httpx2
from openai import AsyncOpenAI, APIStatusError, APIConnectionError, APITimeoutError, APIResponseValidationError
import tiktoken
from pydantic import Field
from logquant.ingestion.models import Contract
from .prompts import AgentRequest, PromptBody, canonical

MODEL = "gpt-5.4-mini-2026-03-17"
TOKENIZER_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
TOKENIZER_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
CACHE = Path(__file__).resolve().parents[3]/".analysis-cache/tiktoken"


def strict_schema(schema: dict) -> dict:
    """Required properties and no defaults; application validators remain authoritative."""
    value = deepcopy(schema)
    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            # Keep the wire dialect within the documented subset. Local Pydantic
            # still checks nonempty strings and every application-level invariant.
            node.pop("minLength", None)
            node.pop("maxLength", None)
            if "const" in node: node["enum"] = [node.pop("const")]
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for child in node.values(): walk(child)
        elif isinstance(node, list):
            for child in node: walk(child)
    walk(value)
    return value


def wire_body(model: str, body: PromptBody) -> dict:
    return dict(model=model, messages=[m.model_dump() for m in body.messages], n=1,
        reasoning_effort="none", stream=False, store=False,
        response_format={"type":"json_schema","json_schema":{"name":"logquant_response_v1",
                         "strict":True,"schema":strict_schema(body.response_schema)}})


class ConservativeCounter:
    unit = "model_tokens"
    def __init__(self, model: str):
        if model != MODEL: raise ValueError("Model has no qualified local adapter/counter profile")
        path = CACHE/hashlib.sha1(TOKENIZER_URL.encode()).hexdigest()
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != TOKENIZER_SHA256:
            raise ValueError("Tokenizer cache missing/corrupt; run scripts/cache_analysis_tokenizer.py (no model call)")
        os.environ["TIKTOKEN_CACHE_DIR"] = str(CACHE)
        self.encoding = tiktoken.encoding_for_model(model)
        if self.encoding.name != "o200k_base": raise ValueError("Unexpected model tokenizer mapping")
        self.model = model
        self.identity = f"{model}:o200k_base:wire-json*1.25+1024:estimate-v1"

    def count_text(self, text: str) -> int:
        return len(self.encoding.encode(text, disallowed_special=()))

    def count_request(self, body: PromptBody) -> int:
        # The wire JSON includes instructions, evidence, schema, triage and repairs.
        # 25% plus 1024 covers unknown server-side schema/message framing heuristically.
        return math.ceil(self.count_text(canonical(wire_body(self.model, body)))*1.25)+1024


class Usage(Contract):
    input_tokens: int | None = Field(default=None,ge=0)
    output_tokens: int | None = Field(default=None,ge=0)
    total_tokens: int | None = Field(default=None,ge=0)
    cached_input_tokens: int | None = Field(default=None,ge=0)
    reasoning_tokens: int | None = Field(default=None,ge=0)


class ProviderOutcome(Contract):
    status: Literal["completed","refusal","truncated","provider_error","rate_limited","timeout","cancelled","malformed_provider_response","model_mismatch"]
    raw_output: str | None = None
    refusal: str | None = None
    request_id: str | None = None
    response_id: str | None = None
    returned_model: str | None = None
    finish_reason: str | None = None
    http_status: int | None = None
    usage: Usage = Field(default_factory=Usage)
    latency_seconds: float = 0.0
    error_code: str | None = None


class OpenAIAdapter:
    def __init__(self, *, model: str, api_key: str, transport=None):
        if model != MODEL: raise ValueError("Explicit supported snapshot model ID is required")
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url="https://api.openai.com/v1", max_retries=0,
            http_client=httpx2.AsyncClient(transport=transport or httpx2.AsyncHTTPTransport(retries=0),
                                          trust_env=False, follow_redirects=False),
            _strict_response_validation=True)

    async def close(self) -> None:
        await self.client.close()

    async def generate(self, request: AgentRequest, client_request_id: str) -> ProviderOutcome:
        started = perf_counter()
        try:
            async with asyncio.timeout(request.timeout_seconds):
                response = await self.client.chat.completions.create(**wire_body(self.model, request.body),
                    max_completion_tokens=request.max_output_tokens, timeout=request.timeout_seconds,
                    extra_headers={"X-Client-Request-Id":client_request_id})
            u = response.usage
            usage = Usage(input_tokens=getattr(u,"prompt_tokens",None), output_tokens=getattr(u,"completion_tokens",None),
                total_tokens=getattr(u,"total_tokens",None),
                cached_input_tokens=getattr(getattr(u,"prompt_tokens_details",None),"cached_tokens",None),
                reasoning_tokens=getattr(getattr(u,"completion_tokens_details",None),"reasoning_tokens",None))
            common = dict(request_id=getattr(response,"_request_id",None),response_id=response.id,
                          returned_model=response.model,usage=usage)
            if len(response.choices) != 1:
                outcome = ProviderOutcome(status="malformed_provider_response",**common)
            else:
                choice = response.choices[0]; message = choice.message
                status = ("model_mismatch" if response.model != self.model else "refusal" if message.refusal or choice.finish_reason == "content_filter"
                          else "truncated" if choice.finish_reason == "length" else "completed" if choice.finish_reason == "stop" and isinstance(message.content,str)
                          else "malformed_provider_response")
                outcome = ProviderOutcome(status=status,raw_output=message.content,refusal=message.refusal,
                    finish_reason=choice.finish_reason,**common)
        except asyncio.CancelledError:
            outcome = ProviderOutcome(status="cancelled",error_code="local_task_cancelled_provider_billing_unknown")
        except (TimeoutError, APITimeoutError):
            outcome = ProviderOutcome(status="timeout",error_code="deadline_or_sdk_timeout_provider_billing_unknown")
        except APIStatusError as exc:
            # Never persist exception strings, bodies or headers (authentication errors can echo keys).
            outcome = ProviderOutcome(status="rate_limited" if exc.status_code == 429 else "provider_error",
                http_status=exc.status_code,request_id=exc.request_id,error_code=type(exc).__name__)
        except APIResponseValidationError as exc:
            outcome = ProviderOutcome(status="malformed_provider_response",error_code="sdk_response_validation",
                request_id=getattr(getattr(exc,"response",None),"headers",{}).get("x-request-id"))
        except APIConnectionError:
            outcome = ProviderOutcome(status="provider_error",error_code="connection_error_usage_unknown")
        except (ValueError, TypeError, AttributeError):
            outcome = ProviderOutcome(status="malformed_provider_response",error_code="malformed_response")
        return outcome.model_copy(update={"latency_seconds":perf_counter()-started})
