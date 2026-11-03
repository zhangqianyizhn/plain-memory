import json
import time

import httpx
import pytest
from pydantic import ValidationError

from plain_memory.config import LLMConfig
from plain_memory.llm import LLMError, NoneLinearLLM


def response(content="NONELINEAR_OK", *, finish="stop", model="openai/gpt-4o-mini"):
    return {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 16, "completion_tokens": 5, "total_tokens": 21},
    }


def test_nonelinear_request_matches_existing_working_integration():
    messages = [
        {"role": "system", "content": "Use supplied evidence only."},
        {"role": "user", "content": "Reply with exactly: NONELINEAR_OK"},
    ]
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://api.nonelinear.com.cn/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-nonelinear-key"
        assert json.loads(request.content) == {
            "model": "gpt-4o-mini",
            "messages": messages,
            "temperature": 0,
            "max_tokens": 64,
            "stream": False,
        }
        return httpx.Response(200, json=response())

    llm = NoneLinearLLM(
        LLMConfig(api_key="synthetic-nonelinear-key", max_tokens=64),
        transport=httpx.MockTransport(handle),
    )
    try:
        actual = llm.complete(messages)
        assert actual.content == "NONELINEAR_OK"
        assert actual.model == "openai/gpt-4o-mini"
        assert actual.usage["total_tokens"] == 21
        assert len(calls) == 1
    finally:
        llm.close()


@pytest.mark.parametrize(
    "body,kind",
    [
        (response(content=""), "empty_content"),
        (response(finish="length"), "incomplete_output"),
        (response(model="different-model"), "unexpected_model"),
        ({"choices": []}, "invalid_response"),
    ],
)
def test_unusable_responses_do_not_silently_succeed(body, kind):
    llm = NoneLinearLLM(
        LLMConfig(api_key="synthetic-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    try:
        with pytest.raises(LLMError) as error:
            llm.complete([{"role": "user", "content": "synthetic"}])
        assert error.value.kind == kind
    finally:
        llm.close()


@pytest.mark.parametrize("status", [401, 429, 503])
def test_upstream_errors_do_not_retry_or_expose_keys_and_prompt(status):
    calls = []

    def fail(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "PRIVATE_CONTENT_AND_KEY"})

    llm = NoneLinearLLM(LLMConfig(api_key="synthetic-key"), transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(LLMError) as error:
            llm.complete([{"role": "user", "content": "PRIVATE_PROMPT"}])
        assert error.value.upstream_status == status
        assert "PRIVATE" not in str(error.value) and "synthetic-key" not in str(error.value)
        assert len(calls) == 1
    finally:
        llm.close()


def test_model_and_endpoint_fixed_and_placeholder_key_rejected():
    for values in ({"model": "different"}, {"base_url": "https://different.invalid/v1"}):
        with pytest.raises(ValidationError):
            LLMConfig(**values)
    with pytest.raises(ValueError):
        NoneLinearLLM(LLMConfig())


def test_expired_deadline_makes_no_request():
    calls = []
    llm = NoneLinearLLM(
        LLMConfig(api_key="synthetic-key"),
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    )
    try:
        with pytest.raises(LLMError, match="deadline"):
            llm.complete([], deadline=time.monotonic() - 1)
        assert not calls
    finally:
        llm.close()


def test_longer_default_timeout_respects_callers_shorter_deadline():
    config = LLMConfig(api_key="synthetic-key")
    assert config.timeout_seconds == 180
    observed = []

    def handle(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, json=response())

    llm = NoneLinearLLM(config, transport=httpx.MockTransport(handle))
    try:
        llm.complete([{"role": "user", "content": "synthetic"}])
        assert observed[0]["read"] == 180
        assert observed[0]["connect"] == 5
        llm.complete([{"role": "user", "content": "synthetic"}], deadline=time.monotonic() + 2)
        assert 0 < observed[1]["read"] <= 2
        assert 0 < observed[1]["connect"] <= 2
    finally:
        llm.close()
