import pytest

import tools
from auditor.context import Turn, build_messages
from auditor.llm import LLMError, ToolCall, ToolUseFailed, estimate_tokens
from auditor.loop import AuditLoop, LoopConfig, Trace
from auditor.prompts import SYSTEM_PROMPT
from auditor.state import AREAS, AuditState
from tests.conftest import SAMPLE_COMMITS
from tests.fakes import FakeLLM, call, reply


@pytest.fixture
def state(git_repo_factory):
    return AuditState(repo=git_repo_factory(SAMPLE_COMMITS))


def run(state, script, tmp_path=None, **cfg):
    llm = FakeLLM(script)
    trace = Trace(str(tmp_path / "t.jsonl") if tmp_path else None)
    result = AuditLoop(state, llm, LoopConfig(**cfg), trace).run()
    trace.close()
    return result, llm, trace


def cover_all():
    return [reply(call("update_coverage", areas=[a], status="examined", reason="checked")) for a in AREAS]


def test_dynamic_investigation_records_and_finishes(state, tmp_path):
    script = [
        reply(call("inspect_workflows", reason="profile shows one workflow")),
        # The next choice depends on the previous result: the model saw line 13.
        lambda msgs: (
            reply(
                call(
                    "record_violation",
                    rule_id="CI-002",
                    category="ci",
                    severity="high",
                    file=".github/workflows/ci.yml",
                    line=13,
                    description="Test failures ignored",
                    evidence="run step is 'pytest || true'",
                    recommendation="Remove '|| true'",
                    kind="present",
                    confidence="high",
                )
            )
            if "pytest || true  [line 13]" in msgs[-1]["content"]
            else reply(call("finish", summary="unexpected"))
        ),
        reply(call("read_file", path="app/config.py", reason="config may hold secrets")),
        reply(
            call(
                "record_violation",
                rule_id="SEC-001",
                category="security",
                severity="critical",
                file="app/config.py",
                line=4,
                description="Hardcoded API key",
                evidence="API_KEY assigned a literal",
                recommendation="Use an env var",
                kind="present",
                confidence="high",
            )
        ),
        *cover_all(),
        reply(call("finish", summary="done")),
        # review phase
        reply(call("finish", summary="all good")),
    ]
    result, llm, trace = run(state, script, tmp_path)
    assert result.stop_reason == "finished" and result.reviewed
    ids = [c.data["rule_id"] for c in state.active_candidates()]
    assert ids == ["CI-002", "SEC-001"]
    tool_records = [r for r in trace.records if "tool" in r]
    assert tool_records[0]["args"]["reason"] == "profile shows one workflow"
    assert (tmp_path / "t.jsonl").read_text().count("\n") == len(trace.records)
    assert "record_violation" not in llm.tools_seen[-1]  # review phase tool set


def test_rejected_violation_is_fed_back(state):
    seen = []
    script = [
        reply(
            call(
                "record_violation",
                rule_id="SEC-001",
                category="security",
                severity="critical",
                file="app/config.py",
                line=4,
                description="d",
                evidence="e",
                recommendation="r",
                kind="present",
                confidence="high",
            )
        ),
        lambda msgs: seen.append(msgs[-1]["content"]) or reply(call("finish", summary="x")),
        reply(call("finish", summary="x")),
    ]
    run(state, script)
    assert "REJECTED" in seen[0] and "have not looked" in seen[0]
    assert state.active_candidates() == []


def test_finish_with_pending_areas_gets_one_reminder(state):
    script = [reply(call("finish", summary="early")), reply(call("finish", summary="again"))]
    result, llm, _ = run(state, script)
    assert result.stop_reason == "finished"
    assert "not examined yet" in llm.requests[1][-1]["content"]


def test_max_steps(state):
    script = [reply(call("list_files", reason="look")) for _ in range(10)]
    result, llm, _ = run(state, script, max_steps=4)
    assert result.stop_reason == "max_steps" and result.steps == 4
    assert len(llm.requests) == 4  # no review: no candidates


def test_budget_low_warning(state):
    script = [reply(call("list_files", reason="look")) for _ in range(4)]
    _, llm, _ = run(state, script, max_steps=4)
    assert "Budget low" in llm.requests[3][1]["content"]
    assert "Budget low" not in llm.requests[0][1]["content"]


def test_tool_use_failed_recovery(state):
    script = [ToolUseFailed("Failed to call a function"), reply(call("list_files", reason="x"))]
    _, llm, trace = run(state, script, max_steps=2)
    assert "malformed" in llm.requests[1][-1]["content"]
    # The failed call is not in the history; only the reminder is.
    assert all(m["role"] != "assistant" for m in llm.requests[1])
    assert any(r.get("event") == "tool_use_failed" for r in trace.records)


def test_tool_use_failed_too_often(state):
    with pytest.raises(LLMError):
        run(state, [ToolUseFailed("x")] * 5)


def test_no_tool_calls_three_times_stops(state):
    result, llm, _ = run(state, [reply(content="I think it's fine")] * 3)
    assert result.stop_reason == "no_tool_calls"
    assert llm.requests[1][-1]["content"].startswith("You must answer with a tool call")
    assert llm.requests[1][-2] == {"role": "assistant", "content": "I think it's fine"}


def test_unknown_tool_and_bad_args_do_not_crash(state):
    script = [reply(call("rm_rf", path="/")), reply(call("read_file", path="../../etc/passwd", reason="x"))]
    _, llm, trace = run(state, script, max_steps=3)
    results = [r["result_preview"] for r in trace.records if "tool" in r]
    assert "not available" in results[0]
    assert "outside the repository" in results[1]
    # The invented tool is never replayed to the API; a correction is sent instead.
    assert "do not exist" in llm.requests[1][-1]["content"]
    assert all("rm_rf" not in str(m.get("tool_calls", "")) for r in llm.requests for m in r)


def test_repeat_detection(state):
    llm = FakeLLM(
        [
            reply(call("read_file", path="README.md", reason="first")),
            reply(call("read_file", path="./README.md", reason="again, other words")),
            reply(call("read_file", path="README.md", start_line=2, reason="different range")),
        ]
    )
    loop = AuditLoop(state, llm, LoopConfig(max_steps=3), Trace(None))
    loop.run()
    results = [r["result_preview"] for r in loop.trace.records if "tool" in r]
    assert results[0].startswith("README.md  lines 1-3")
    assert results[1].startswith("REPEATED CALL") and "README.md  lines 1-3" in results[1]
    assert results[2].startswith("README.md  lines 2-3")
    assert loop.repeats == 1
    assert (
        "Files already seen" in llm.requests[1][1]["content"]
        and "README.md (all)" in llm.requests[1][1]["content"]
    )


def test_time_limit_skips_review(state):
    t = [0.0]

    def clock():
        t[0] += 50
        return t[0]

    llm = FakeLLM([reply(call("list_files", reason="x"))] * 50)
    result = AuditLoop(state, llm, LoopConfig(max_seconds=200), Trace(None), clock=clock).run()
    assert result.stop_reason == "time" and not result.reviewed


def test_review_can_retract(state):
    tools_ctx = tools.ToolContext(state=state)
    tools.read_file(tools_ctx, "app/config.py")
    script = [
        reply(
            call(
                "record_violation",
                rule_id="SEC-008",
                category="security",
                severity="low",
                file="app/config.py",
                line=3,
                description="DEBUG on",
                evidence="DEBUG = True",
                recommendation="Use env",
                kind="present",
                confidence="medium",
            )
        ),
        *cover_all(),
        reply(call("finish", summary="done")),
        lambda msgs: (
            reply(call("retract_violation", id="V1", reason="dev only"))
            if "V1 SEC-008" in msgs[1]["content"] and "line text: DEBUG = True" in msgs[1]["content"]
            else reply(call("finish", summary="candidate details missing"))
        ),
        reply(call("finish", summary="reviewed")),
    ]
    result, _, trace = run(state, script)
    assert result.reviewed
    assert state.active_candidates() == []
    end = trace.records[-1]
    assert end["retracted"][0]["reason"] == "dev only"


def test_messages_stay_in_budget_and_compact_old_turns(state):
    ctx = tools.ToolContext(state=state)
    turns = []
    for i in range(1, 9):
        c = ToolCall(id=f"c{i}", name="read_file", arguments='{"path":"app/config.py","reason":"r"}')
        turns.append(
            Turn(
                step=i,
                calls=[c],
                results=[tools.read_file(ctx, "app/config.py") * 8],
                args=[{"path": "app/config.py", "reason": "r"}],
                ok=[True],
            )
        )
    schemas = tools.tool_schemas(tools.tool_names(False))
    msgs, n_full = build_messages(SYSTEM_PROMPT, schemas, state, turns, 9, 40, 0.5, prompt_budget=5200)
    assert 1 <= n_full <= 3
    total = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(schemas) + estimate_tokens(msgs[1:])
    assert total <= 5200 + 300
    assert 's1 read_file(path="app/config.py")' in msgs[1]["content"]


def test_newest_turn_is_cut_when_too_big(state):
    big = "x" * 40000
    turns = [Turn(step=1, calls=[ToolCall("c1", "list_files", "{}")], results=[big], args=[{}], ok=[True])]
    msgs, n_full = build_messages(SYSTEM_PROMPT, [], state, turns, 2, 40, 1.0, prompt_budget=5200)
    assert n_full == 1
    assert "cut to fit" in msgs[-1]["content"] and len(msgs[-1]["content"]) < 20000


def test_system_prompt_size():
    assert estimate_tokens(SYSTEM_PROMPT) < 2000
    assert estimate_tokens(tools.tool_schemas(tools.tool_names(True))) < 2600
