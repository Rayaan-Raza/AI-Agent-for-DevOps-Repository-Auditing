"""Scripted fake LLM for loop and CLI tests. No network."""

from __future__ import annotations

import json

from auditor.llm import LLMResponse, ToolCall, ToolUseFailed


def call(name: str, **args) -> ToolCall:
    call.n += 1
    return ToolCall(id=f"call_{call.n}", name=name, arguments=json.dumps(args))


call.n = 0


def reply(*calls: ToolCall, content: str = "") -> LLMResponse:
    return LLMResponse(content=content, tool_calls=list(calls), usage={"prompt": 100, "completion": 10})


class FakeLLM:
    """Return scripted responses in order. An item can be an LLMResponse, an exception to raise,
    or a callable(messages) -> LLMResponse. After the script ends, it calls finish."""

    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[list[dict]] = []
        self.tools_seen: list[list[str]] = []

    def complete(self, messages, tools):
        self.requests.append(messages)
        self.tools_seen.append([t["function"]["name"] for t in tools])
        _check_message_shape(messages)
        if not self.script:
            return reply(call("finish", summary="script ended"))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(messages)
        return item


def _check_message_shape(messages):
    """The same structural rules the OpenAI/Groq API applies."""
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    open_ids: set[str] = set()
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            assert not open_ids, "previous tool calls were not answered"
            open_ids = {tc["id"] for tc in m["tool_calls"]}
        elif m["role"] == "tool":
            assert m["tool_call_id"] in open_ids, "tool result without a matching call"
            open_ids.discard(m["tool_call_id"])
        else:
            assert not open_ids, "tool calls must be followed by their results"
    assert not open_ids


__all__ = ["FakeLLM", "ToolUseFailed", "call", "reply"]
