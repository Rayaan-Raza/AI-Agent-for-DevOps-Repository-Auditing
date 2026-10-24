import httpx
import pytest

from auditor import llm as llm_mod
from auditor.llm import GroqClient, LLMAuthError, LLMError, RateLimiter, ToolUseFailed, parse_duration


@pytest.mark.parametrize(
    "value,seconds",
    [("3.142s", 3.142), ("1m26.4s", 86.4), ("2h0m1s", 7201), ("850ms", 0.85), ("12", 12), (None, 0), ("", 0)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == pytest.approx(seconds)


def test_rate_limiter_waits_only_when_needed():
    now = [100.0]
    slept = []
    rl = RateLimiter(sleep=lambda s: slept.append(s), clock=lambda: now[0])
    assert rl.wait_for(5000) == 0  # nothing known yet
    rl.update(
        {
            "x-ratelimit-remaining-tokens": "7000",
            "x-ratelimit-reset-tokens": "2s",
            "x-ratelimit-remaining-requests": "900",
            "x-ratelimit-reset-requests": "1m",
        }
    )
    assert rl.wait_for(5000) == 0
    rl.update({"x-ratelimit-remaining-tokens": "1000", "x-ratelimit-reset-tokens": "7.5s"})
    assert rl.wait_for(5000) == pytest.approx(8.0)
    assert slept == [pytest.approx(8.0)]
    rl.update(
        {
            "x-ratelimit-remaining-tokens": "8000",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "500s",
        }
    )
    assert rl.wait_for(10) == 90.0  # capped


def test_estimate_tokens_is_conservative():
    text = "word " * 1000
    assert llm_mod.estimate_tokens(text) >= 1000


def test_missing_key():
    with pytest.raises(LLMAuthError):
        GroqClient(api_key="")


# --- error mapping with a stubbed Groq SDK --------------------------------------------


def _resp(status, body=None, headers=None):
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return httpx.Response(status, request=req, json=body or {}, headers=headers or {})


class _Raw:
    def __init__(self, parsed, headers):
        self._parsed, self.headers = parsed, headers

    def parse(self):
        return self._parsed


def _client_raising(monkeypatch, errors, success=None):
    client = GroqClient(api_key="gsk_test_key_value_123456", log=lambda m: None)
    seq = list(errors)

    def create(**kwargs):
        if seq:
            raise seq.pop(0)
        return success

    monkeypatch.setattr(client.client.chat.completions.with_raw_response, "create", create)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    client.limiter._sleep = lambda s: None
    return client


def test_tool_use_failed_is_mapped(monkeypatch):
    import groq

    body = {
        "error": {
            "message": "Failed to call a function.",
            "code": "tool_use_failed",
            "failed_generation": "<function=x>",
        }
    }
    err = groq.BadRequestError("bad", response=_resp(400, body), body=body)
    client = _client_raising(monkeypatch, [err])
    with pytest.raises(ToolUseFailed) as e:
        client.complete([{"role": "user", "content": "x"}], [])
    assert "<function=x>" in e.value.failed_generation


def test_auth_error_is_mapped(monkeypatch):
    import groq

    err = groq.AuthenticationError("no", response=_resp(401), body=None)
    client = _client_raising(monkeypatch, [err])
    with pytest.raises(LLMAuthError):
        client.complete([], [])


def test_daily_limit_stops(monkeypatch):
    import groq

    err = groq.RateLimitError("429", response=_resp(429, headers={"retry-after": "3600"}), body=None)
    client = _client_raising(monkeypatch, [err])
    with pytest.raises(LLMError, match="daily"):
        client.complete([], [])


def test_short_rate_limit_then_success(monkeypatch):
    from types import SimpleNamespace as NS

    import groq

    parsed = NS(
        choices=[
            NS(
                message=NS(
                    content="",
                    reasoning="thinking",
                    tool_calls=[NS(id="c1", function=NS(name="finish", arguments='{"summary":"x"}'))],
                )
            )
        ],
        usage=NS(prompt_tokens=10, completion_tokens=5),
    )
    err = groq.RateLimitError("429", response=_resp(429, headers={"retry-after": "2"}), body=None)
    client = _client_raising(
        monkeypatch, [err], success=_Raw(parsed, {"x-ratelimit-remaining-tokens": "7000"})
    )
    out = client.complete([{"role": "user", "content": "x"}], [])
    assert out.tool_calls[0].name == "finish" and out.reasoning == "thinking"
    assert out.usage == {"prompt": 10, "completion": 5}
    assert client.limiter.remaining_tokens == 7000
    assert client.calibration < 1.0  # real usage was below the estimate


def test_reasoning_effort_only_for_supported_models():
    assert GroqClient(api_key="k" * 10, model="openai/gpt-oss-20b").reasoning_effort == "medium"
    assert GroqClient(api_key="k" * 10, model="llama-3.3-70b-versatile").reasoning_effort is None


def test_length_cutoff_retries_with_more_tokens(monkeypatch):
    from auditor.llm import LLMResponse, ToolCall

    client = GroqClient(api_key="k" * 10, log=lambda m: None)
    seen = []

    def fake_request(messages, tools, max_tokens):
        seen.append(max_tokens)
        if len(seen) == 1:
            return LLMResponse(content="", tool_calls=[], finish_reason="length")
        return LLMResponse(content="", tool_calls=[ToolCall("c", "finish", "{}")], finish_reason="tool_calls")

    monkeypatch.setattr(client, "_request", fake_request)
    out = client.complete([], [])
    assert out.tool_calls and seen == [1000, 3000]
