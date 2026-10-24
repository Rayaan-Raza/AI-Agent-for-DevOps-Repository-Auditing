"""The agent loop: Phase 1 (investigation) and Phase 2 (self-review).

Cycle: observe (audit state + last results) -> reason (LLM) -> select tool (tool call)
-> execute (Python) -> interpret (next LLM turn) -> ... -> finish -> report.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import tools as toolbox
from auditor import prompts
from auditor.context import Turn, build_messages
from auditor.llm import LLMClient, ToolUseFailed
from auditor.redact import redact, redact_obj
from auditor.state import AuditState

REVIEW_TOOLS = [
    "read_file",
    "search_code",
    "list_files",
    "git_log",
    "inspect_workflows",
    "inspect_dependencies",
    "git_tracked_ignored",
    "update_violation",
    "retract_violation",
    "finish",
]


@dataclass
class LoopConfig:
    max_steps: int = 35
    max_seconds: float = 1800.0
    review_steps: int = 8
    prompt_budget: int = 4000
    window: int = 6
    allow_exec: bool = False
    max_tool_failures: int = 3
    max_no_tool: int = 3


@dataclass
class LoopResult:
    state: AuditState
    steps: int
    stop_reason: str  # finished | max_steps | time | no_tool_calls
    reviewed: bool


class Trace:
    """Write one JSON record per line. Every record is redacted."""

    def __init__(self, path: str | None):
        self.path = path
        self._fh = open(path, "w", encoding="utf-8") if path else None  # noqa: SIM115
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        record = redact_obj(record)
        self.records.append(record)
        if self._fh:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class AuditLoop:
    def __init__(
        self,
        state: AuditState,
        llm: LLMClient,
        config: LoopConfig,
        trace: Trace,
        log=None,
        clock=time.monotonic,
    ):
        self.state = state
        self.llm = llm
        self.cfg = config
        self.trace = trace
        self.log = log or (lambda msg: None)
        self.clock = clock
        self.start = clock()
        self.ctx = toolbox.ToolContext(state=state, allow_exec=config.allow_exec)
        self.llm_calls = 0
        self.repeats = 0
        self.done_calls: dict[str, tuple[int, str]] = {}

    # ------------------------------------------------------------ helpers --
    def time_left_frac(self) -> float:
        return max(0.0, 1 - (self.clock() - self.start) / self.cfg.max_seconds)

    def out_of_time(self) -> bool:
        return self.clock() - self.start >= self.cfg.max_seconds

    def _ask(
        self,
        phase: str,
        step: int,
        max_steps: int,
        tool_list: list[str],
        turns: list[Turn],
        extra: list[str],
        tail: list[dict],
    ):
        schemas = toolbox.tool_schemas(tool_list)
        messages, n_full = build_messages(
            prompts.SYSTEM_PROMPT,
            schemas,
            self.state,
            turns,
            step,
            max_steps,
            self.time_left_frac(),
            self.cfg.prompt_budget,
            window=self.cfg.window,
            extra=extra,
            tail=tail,
        )
        self.llm_calls += 1
        t0 = self.clock()
        resp = self.llm.complete(messages, schemas)
        return resp, self.clock() - t0, n_full

    def _call_key(self, name: str, raw: str) -> str | None:
        """Identity of an investigation call (arguments without 'reason'), for repeat detection."""
        if name not in toolbox.TOOL_DEFS or not toolbox.TOOL_DEFS[name][3]:
            return None
        try:
            args = toolbox.parse_arguments(name, raw)
        except toolbox.ToolError:
            return None
        args.pop("reason", None)
        if isinstance(args.get("path"), str):
            p = args["path"].strip().replace("\\", "/")
            while p.startswith("./"):
                p = p[2:]
            args["path"] = p or "."
        if name == "read_file" and args.get("start_line") == 1:
            args.pop("start_line")
        return name + json.dumps(args, sort_keys=True)

    def _run_calls(self, phase: str, step: int, resp, elapsed: float, allowed: list[str]) -> Turn:
        turn = Turn(step=step, calls=[], results=[], args=[], ok=[])
        for call in resp.tool_calls:
            self.ctx.step = step
            key = self._call_key(call.name, call.arguments)
            if call.name not in allowed:
                text, args, ok = f"ERROR: tool {call.name!r} is not available in this phase.", {}, False
            elif key and key in self.done_calls:
                prev_step, prev_text = self.done_calls[key]
                self.repeats += 1
                args = toolbox.parse_arguments(call.name, call.arguments)
                text = (
                    f"REPEATED CALL: you already ran this exact call at step {prev_step}. Do not repeat "
                    "calls. Act on the result now: record_violations, add_note, update_coverage, or "
                    f"investigate a different file or area.\nResult of step {prev_step}:\n" + prev_text[:2500]
                )
                ok = False
            else:
                text, args, ok = toolbox.execute(self.ctx, call.name, call.arguments)
                if key and ok:
                    self.done_calls[key] = (step, text)
            turn.calls.append(call)
            turn.results.append(text)
            turn.args.append(args)
            turn.ok.append(ok)
            reason = args.get("reason", "")
            self.log(
                f"[{phase} {step}] {call.name} {'ok' if ok else 'ERR'}"
                + (f" - {reason[:100]}" if reason else "")
            )
            self.trace.write(
                {
                    "step": step,
                    "phase": phase,
                    "tool": call.name,
                    "args": args or {"raw": call.arguments[:300]},
                    "ok": ok,
                    "result_chars": len(text),
                    "result_preview": text[:300],
                    "model_reasoning": (resp.reasoning or "")[:600],
                    "tokens": resp.usage,
                    "llm_seconds": round(elapsed, 2),
                    "elapsed_s": round(self.clock() - self.start, 1),
                }
            )
        return turn

    # ------------------------------------------------------------ phase 1 --
    def investigate(self) -> tuple[str, int]:
        turns: list[Turn] = []
        tail: list[dict] = []
        step = 0
        no_tool = tool_fail = 0
        finish_reminded = False
        allowed = toolbox.tool_names(self.cfg.allow_exec, phase="investigation")
        while True:
            if self.out_of_time():
                return "time", step
            if step >= self.cfg.max_steps:
                return "max_steps", step
            step += 1
            extra = []
            left = self.cfg.max_steps - step
            if step >= 0.6 * self.cfg.max_steps:
                extra.append(prompts.BUDGET_LOW.format(left=left))
            if self.repeats >= 2:
                extra.append(prompts.LOOPING.format(n=self.repeats))
            try:
                resp, elapsed, _ = self._ask(
                    "investigation", step, self.cfg.max_steps, allowed, turns, extra, tail
                )
            except ToolUseFailed as e:
                tool_fail += 1
                self.trace.write(
                    {
                        "step": step,
                        "phase": "investigation",
                        "event": "tool_use_failed",
                        "error": str(e)[:300],
                        "failed_generation": e.failed_generation[:800],
                    }
                )
                self.log(f"[investigation {step}] malformed tool call ({tool_fail})")
                if tool_fail > self.cfg.max_tool_failures:
                    # Keep the work done so far: stop investigating and go on to the review and report.
                    return "tool_failures", step
                hint = prompts.TOOL_USE_FAILED.format(error=str(e)[:150])
                if tool_fail >= 2:
                    hint += " " + prompts.TOOL_USE_FAILED_AGAIN
                tail = [{"role": "user", "content": hint}]
                continue
            tool_fail = 0
            if not resp.tool_calls:
                no_tool += 1
                self.trace.write(
                    {
                        "step": step,
                        "phase": "investigation",
                        "event": "no_tool_call",
                        "content": resp.content[:300],
                        "finish_reason": getattr(resp, "finish_reason", ""),
                        "reasoning": (resp.reasoning or "")[:600],
                        "tokens": resp.usage,
                    }
                )
                if no_tool >= self.cfg.max_no_tool:
                    return "no_tool_calls", step
                tail = []
                if resp.content.strip():
                    tail.append({"role": "assistant", "content": resp.content[:1000]})
                tail.append({"role": "user", "content": prompts.NO_TOOL_CALL})
                continue
            no_tool = 0
            tail = []
            turn = self._run_calls("investigation", step, resp, elapsed, allowed)
            bad = [c.name for c in turn.calls if c.name not in allowed]
            if bad:
                # Groq rejects a history that calls a tool which is not in the request.
                tail = [
                    {
                        "role": "user",
                        "content": f"Tool(s) {bad} do not exist. Use only: {', '.join(allowed)}.",
                    }
                ]
                continue
            turns.append(turn)
            if self.state.finished:
                pending = self.state.pending_areas()
                if pending and not finish_reminded:
                    finish_reminded = True
                    self.state.finished = False
                    turn.results[-1] += "\n" + prompts.FINISH_WITH_PENDING.format(areas=", ".join(pending))
                    continue
                return "finished", step

    # ------------------------------------------------------------ phase 2 --
    def _candidate_details(self) -> str:
        rows = []
        for c in self.state.active_candidates():
            d = c.data
            loc = (d["file"] or "(no file)") + (f":{d['line']}" if d["line"] else "")
            row = (
                f"{c.id} {d['rule_id']} {d['category']} {d['severity']} {loc} "
                f"conf={d.get('confidence')}\n  description: {d['description']}\n"
                f"  evidence: {d['evidence']}"
            )
            if d["file"] and d["line"] and d["file"] in self.state.repo.files:
                try:
                    lines = toolbox.read_lines(self.ctx, d["file"])
                    row += f"\n  line text: {redact(lines[d['line'] - 1].strip(), path=d['file'])[:160]}"
                except (toolbox.ToolError, IndexError, OSError):
                    pass
            rows.append(row)
        return "\n".join(rows)

    def review(self) -> bool:
        if not self.state.active_candidates() or self.out_of_time():
            return False
        self.state.finished = False
        turns: list[Turn] = []
        tail: list[dict] = []
        tool_fail = no_tool = 0
        for step in range(1, self.cfg.review_steps + 1):
            if self.out_of_time():
                break
            extra = [
                prompts.REVIEW_PROMPT.format(
                    steps=self.cfg.review_steps - step + 1, candidates=self._candidate_details()
                )
            ]
            try:
                resp, elapsed, _ = self._ask(
                    "review", step, self.cfg.review_steps, REVIEW_TOOLS, turns, extra, tail
                )
            except ToolUseFailed as e:
                tool_fail += 1
                self.trace.write(
                    {
                        "step": step,
                        "phase": "review",
                        "event": "tool_use_failed",
                        "error": str(e)[:300],
                        "failed_generation": e.failed_generation[:800],
                    }
                )
                if tool_fail > self.cfg.max_tool_failures:
                    break
                tail = [{"role": "user", "content": prompts.TOOL_USE_FAILED.format(error=str(e)[:150])}]
                continue
            if not resp.tool_calls:
                no_tool += 1
                if no_tool >= self.cfg.max_no_tool:
                    break
                tail = [{"role": "user", "content": prompts.NO_TOOL_CALL}]
                continue
            tail = []
            turn = self._run_calls("review", step, resp, elapsed, REVIEW_TOOLS)
            bad = [c.name for c in turn.calls if c.name not in REVIEW_TOOLS]
            if bad:
                tail = [
                    {
                        "role": "user",
                        "content": f"Tool(s) {bad} are not available in the review. "
                        f"Use only: {', '.join(REVIEW_TOOLS)}.",
                    }
                ]
                continue
            turns.append(turn)
            if self.state.finished:
                break
        return True

    def run(self) -> LoopResult:
        reason, steps = self.investigate()
        self.log(
            f"investigation ended: {reason} after {steps} steps; "
            f"{len(self.state.active_candidates())} candidates"
        )
        reviewed = False
        if reason != "time":
            reviewed = self.review()
        self.trace.write(
            {
                "event": "end",
                "stop_reason": reason,
                "steps": steps,
                "reviewed": reviewed,
                "llm_calls": self.llm_calls,
                "repeated_calls": self.repeats,
                "elapsed_s": round(self.clock() - self.start, 1),
                "coverage": self.state.coverage,
                "retracted": [
                    {"id": c.id, "rule_id": c.data["rule_id"], "reason": c.retract_reason}
                    for c in self.state.candidates
                    if c.retracted
                ],
            }
        )
        return LoopResult(state=self.state, steps=steps, stop_reason=reason, reviewed=reviewed)
