"""Make the messages for each turn (design section 3.2).

The full history is never sent. Each request has: the system prompt, an AUDIT STATE
message made by Python, and the newest turns (tool calls and results) that fit
in the token budget. Older turns become one summary line each.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from auditor.llm import ToolCall, estimate_tokens
from auditor.repo import profile_text
from auditor.state import AuditState
from tools import VENDORED

MAX_SUMMARY_LINES = 12
MAX_LISTED_FILES = 150


@dataclass
class Turn:
    step: int
    calls: list[ToolCall]
    results: list[str]
    args: list[dict] = field(default_factory=list)
    ok: list[bool] = field(default_factory=list)
    progress: bool = False  # True if the turn gave the LLM new information


def summarize_call(step: int, call: ToolCall, args: dict, result: str, ok: bool) -> str:
    shown = {k: v for k, v in args.items() if k != "reason"}
    arg_text = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in shown.items())
    if len(arg_text) > 90:
        arg_text = arg_text[:87] + "..."
    first = result.strip().splitlines()[0] if result.strip() else ""
    status = "" if ok else " [failed]"
    return f"s{step} {call.name}({arg_text}){status} -> {first[:80]}"


def tracked_files_text(state: AuditState) -> str:
    """All tracked paths for small repositories (vendored directories collapsed)."""
    files = state.repo.files
    if len(files) > MAX_LISTED_FILES:
        return f"Tracked files: {len(files)} (too many to list; use list_files)."
    shown, vendored = [], {}
    for p in sorted(files):
        parts = p.split("/")
        idx = next((i for i, part in enumerate(parts[:-1]) if part in VENDORED), None)
        if idx is None:
            shown.append(p)
        else:
            d = "/".join(parts[: idx + 1]) + "/"
            vendored[d] = vendored.get(d, 0) + 1
    shown += [f"{d} ({n} files)" for d, n in vendored.items()]
    return "Tracked files (complete list): " + ", ".join(shown)


def files_without_verdict(state: AuditState, limit: int = 12) -> list[str]:
    """Files the LLM has seen that no violation names and no note mentions."""
    named = {c.data["file"] for c in state.active_candidates() if c.data.get("file")}
    notes = " ".join(state.notes)
    out = [p for p in sorted(state.seen_lines) if p not in named and p not in notes]
    return out[:limit]


def files_seen_text(state: AuditState, limit: int = 40) -> str:
    parts = []
    for path, lines in sorted(state.seen_lines.items()):
        info = state.repo.files.get(path)
        total = info.lines if info and info.lines else None
        if total and len(lines) >= total:
            parts.append(f"{path} (all)")
        else:
            parts.append(f"{path} ({len(lines)} lines)")
    if len(parts) > limit:
        parts = parts[:limit] + [f"... {len(parts) - limit} more"]
    return ", ".join(parts)


def audit_state_text(
    state: AuditState, step: int, max_steps: int, time_left_frac: float, older: list[str], extra: list[str]
) -> str:
    lines = [
        f"AUDIT STATE (step {step} of {max_steps}, {int(time_left_frac * 100)}% of time budget left)",
        f"Repository: {state.repo.url}",
        "Profile:",
        *("  " + x for x in profile_text(state.repo).splitlines()),
        tracked_files_text(state),
        "Coverage:",
    ]
    for area, status in state.coverage.items():
        reason = state.coverage_reasons.get(area)
        lines.append(f"  {area:<22} {status}" + (f" ({reason[:80]})" if reason else ""))
    lines.append("Notes:" if state.notes else "Notes: (none)")
    lines += [f"  - {n}" for n in state.notes]
    seen = files_seen_text(state)
    if seen:
        lines.append("Files already seen (do not read them again; record what they showed): " + seen)
    pending = files_without_verdict(state)
    if pending:
        lines.append(
            "Files seen with NO verdict yet (record their violations, or add_note '<path>: clean'): "
            + ", ".join(pending)
        )
    active = state.active_candidates()
    lines.append(f"Candidate violations ({len(active)}):")
    for c in active:
        d = c.data
        loc = (d["file"] or "(no file)") + (f":{d['line']}" if d["line"] else "")
        lines.append(f"  {c.id} {d['rule_id']} {d['severity']} {loc}  {d['description'][:70]}")
    if older:
        lines.append("Older steps (results removed from context):")
        if len(older) > MAX_SUMMARY_LINES:
            lines.append(f"  ({len(older) - MAX_SUMMARY_LINES} earlier steps not shown)")
        lines += ["  " + s for s in older[-MAX_SUMMARY_LINES:]]
    lines += extra
    return "\n".join(lines)


def turn_messages(turn: Turn, result_limit: int | None = None) -> list[dict]:
    msgs = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in turn.calls
            ],
        }
    ]
    for c, r in zip(turn.calls, turn.results, strict=True):
        if result_limit is not None and len(r) > result_limit:
            r = r[:result_limit] + "\n[... cut to fit the token budget]"
        msgs.append({"role": "tool", "tool_call_id": c.id, "content": r})
    return msgs


def build_messages(
    system_prompt: str,
    tools: list[dict],
    state: AuditState,
    turns: list[Turn],
    step: int,
    max_steps: int,
    time_left_frac: float,
    prompt_budget: int,
    window: int = 3,
    extra: list[str] | None = None,
    tail: list[dict] | None = None,
) -> tuple[list[dict], int]:
    """Return (messages, number of turns kept in full).

    ``tail`` is appended after the window (for example a reminder from the loop).
    """
    extra = extra or []
    tail = tail or []
    fixed = estimate_tokens(system_prompt) + estimate_tokens(tools) + estimate_tokens(tail)

    def older_lines(n_full: int) -> list[str]:
        older_turns = turns[: len(turns) - n_full] if n_full else turns
        return [
            summarize_call(t.step, c, a, r, ok)
            for t in older_turns
            for c, a, r, ok in zip(
                t.calls, t.args or [{}] * len(t.calls), t.results, t.ok or [True] * len(t.calls), strict=True
            )
        ]

    kept: list[dict] = []
    n_full = 0
    for turn in reversed(turns[-window:] if window else []):
        msgs = turn_messages(turn)
        state_tokens = estimate_tokens(
            audit_state_text(state, step, max_steps, time_left_frac, older_lines(n_full + 1), extra)
        )
        if fixed + state_tokens + estimate_tokens(kept) + estimate_tokens(msgs) <= prompt_budget:
            kept = msgs + kept
            n_full += 1
            continue
        if n_full == 0:
            # Always keep the newest turn: cut its results to fit.
            room = prompt_budget - fixed - state_tokens - 200
            limit = max(800, int(room * 3.2) // max(1, len(turn.calls)))
            kept = turn_messages(turn, result_limit=limit)
            n_full = 1
        break

    state_msg = {
        "role": "user",
        "content": audit_state_text(state, step, max_steps, time_left_frac, older_lines(n_full), extra),
    }
    messages = [{"role": "system", "content": system_prompt}, state_msg, *kept, *tail]
    return messages, n_full
