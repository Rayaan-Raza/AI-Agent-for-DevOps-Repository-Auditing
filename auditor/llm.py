"""LLM access. All provider-specific code is in this module.

The loop only uses ``LLMResponse`` and ``ToolCall``. To change the provider,
write a new client with the same ``complete`` method.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_MODEL = "openai/gpt-oss-120b"
REASONING_MODELS = ("openai/gpt-oss", "qwen/")


class LLMError(Exception):
    """The LLM call failed and cannot continue (exit code 5)."""


class LLMAuthError(LLMError):
    """The API key is missing or rejected (exit code 4)."""


class ToolUseFailed(Exception):
    """The model made a malformed tool call. The loop can ask again."""

    def __init__(self, message: str, failed_generation: str = ""):
        super().__init__(message)
        self.failed_generation = failed_generation


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as the model sent it


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: dict = field(default_factory=dict)
    reasoning: str = ""
    finish_reason: str = ""


class LLMClient(Protocol):
    model: str

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


def estimate_tokens(obj) -> int:
    """Conservative token estimate (about 3.2 characters per token for JSON-heavy text)."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return int(len(text) / 3.2) + 8


def parse_duration(value: str | None) -> float:
    """Parse Groq reset values such as '3.142s', '1m26.4s', '2h0m1s', '850ms'."""
    if not value:
        return 0.0
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


class RateLimiter:
    """Wait before a request when the last response headers show too few tokens or requests."""

    def __init__(self, log=None, sleep=time.sleep, clock=time.monotonic):
        self.remaining_tokens: int | None = None
        self.remaining_requests: int | None = None
        self.token_reset_at = 0.0
        self.request_reset_at = 0.0
        self.limit_tokens: int | None = None
        self._log = log or (lambda msg: None)
        self._sleep = sleep
        self._clock = clock

    def update(self, headers) -> None:
        now = self._clock()

        def _int(name):
            v = headers.get(name)
            try:
                return int(float(v)) if v is not None else None
            except ValueError:
                return None

        self.limit_tokens = _int("x-ratelimit-limit-tokens") or self.limit_tokens
        rt = _int("x-ratelimit-remaining-tokens")
        if rt is not None:
            self.remaining_tokens = rt
            self.token_reset_at = now + parse_duration(headers.get("x-ratelimit-reset-tokens"))
        rr = _int("x-ratelimit-remaining-requests")
        if rr is not None:
            self.remaining_requests = rr
            self.request_reset_at = now + parse_duration(headers.get("x-ratelimit-reset-requests"))

    def wait_for(self, needed_tokens: int) -> float:
        """Sleep if needed. Return the seconds slept."""
        now = self._clock()
        wait = 0.0
        if self.remaining_tokens is not None and self.remaining_tokens < needed_tokens:
            wait = max(wait, self.token_reset_at - now)
        if self.remaining_requests is not None and self.remaining_requests < 1:
            wait = max(wait, self.request_reset_at - now)
        if wait > 0:
            wait = min(wait + 0.5, 90.0)
            self._log(f"rate limit: waiting {wait:.1f} s for tokens")
            self._sleep(wait)
            self.remaining_tokens = None  # unknown until the next response
        return wait


class GroqClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str | None = "medium",
        max_completion_tokens: int = 1000,
        timeout: float = 90.0,
        log=None,
    ):
        try:
            import groq
        except ImportError as e:  # pragma: no cover
            raise LLMError("The 'groq' package is not installed. Run: pip install -r requirements.txt") from e
        if not api_key:
            raise LLMAuthError("GROQ_API_KEY is not set. Copy .env.example to .env and set the key.")
        self._groq = groq
        self.client = groq.Groq(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self.reasoning_effort = reasoning_effort if model.startswith(REASONING_MODELS) else None
        self.max_completion_tokens = max_completion_tokens
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.limiter = RateLimiter(log=self.log)
        self.calibration = 1.0  # real prompt tokens / estimated prompt tokens

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        resp = self._request(messages, tools, self.max_completion_tokens)
        if resp.finish_reason == "length" and not resp.tool_calls:
            # Reasoning used the whole completion budget before the tool call. Try once with more room.
            self.log("completion cut at the token limit; retrying with a larger limit")
            resp = self._request(messages, tools, min(3 * self.max_completion_tokens, 4000))
        return resp

    def _request(self, messages: list[dict], tools: list[dict], max_tokens: int) -> LLMResponse:
        groq = self._groq
        kwargs = dict(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            max_completion_tokens=max_tokens,
            temperature=0.3,
        )
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        estimated_prompt = estimate_tokens(messages) + estimate_tokens(tools)
        needed = int(estimated_prompt * self.calibration * 1.1) + max_tokens

        for attempt in range(5):
            self.limiter.wait_for(needed)
            try:
                raw = self.client.chat.completions.with_raw_response.create(**kwargs)
                self.limiter.update(raw.headers)
                resp = raw.parse()
                break
            except groq.AuthenticationError as e:
                raise LLMAuthError("Groq rejected the API key (401). Check GROQ_API_KEY.") from e
            except groq.PermissionDeniedError as e:
                raise LLMAuthError(f"Groq denied access to model {self.model!r} (403).") from e
            except groq.NotFoundError as e:
                raise LLMError(f"Model {self.model!r} is not available on this Groq account (404).") from e
            except groq.RateLimitError as e:
                retry = parse_duration(e.response.headers.get("retry-after")) if e.response else 0
                if retry > 120:
                    raise LLMError(
                        f"Groq rate limit: retry after {retry:.0f} s. The daily token or "
                        "request limit is probably used up."
                    ) from e
                wait = max(retry, 5.0 * (attempt + 1))
                self.log(f"rate limit (429): waiting {wait:.0f} s")
                time.sleep(wait)
            except groq.BadRequestError as e:
                body = getattr(e, "body", None) or {}
                err = body.get("error", body) if isinstance(body, dict) else {}
                code = err.get("code", "") if isinstance(err, dict) else ""
                msg = (err.get("message") if isinstance(err, dict) else None) or str(e)
                if code == "tool_use_failed" or "tool" in msg.lower() and "call" in msg.lower():
                    raise ToolUseFailed(msg[:300], str(err.get("failed_generation", ""))[:500]) from e
                if "too large" in msg.lower() or "context" in msg.lower() and "length" in msg.lower():
                    raise LLMError(f"Request too large for the model or rate limit: {msg[:200]}") from e
                raise LLMError(f"Groq rejected the request (400): {msg[:300]}") from e
            except groq.APIStatusError as e:
                if e.status_code == 413:
                    raise LLMError("Request too large for the Groq token limit (413).") from e
                if attempt >= 2:
                    raise LLMError(f"Groq API error {e.status_code} after retries.") from e
                time.sleep(5.0 * (attempt + 1))
            except (groq.APITimeoutError, groq.APIConnectionError) as e:
                if attempt >= 2:
                    raise LLMError(f"Groq API not reachable after retries: {type(e).__name__}.") from e
                time.sleep(5.0 * (attempt + 1))
        else:
            raise LLMError("Groq rate limit did not clear after 5 attempts.")

        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
        ]
        usage = {}
        if resp.usage:
            usage = {"prompt": resp.usage.prompt_tokens, "completion": resp.usage.completion_tokens}
            if resp.usage.prompt_tokens and estimated_prompt:
                ratio = resp.usage.prompt_tokens / estimated_prompt
                self.calibration = min(1.5, max(0.3, 0.5 * self.calibration + 0.5 * ratio))
        return LLMResponse(
            content=msg.content or "",
            tool_calls=calls,
            usage=usage,
            reasoning=getattr(msg, "reasoning", None) or "",
            finish_reason=getattr(choice, "finish_reason", None) or "",
        )
