"""Agent state. The LLM changes it only through tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from auditor.repo import Repo

AREAS = [
    "repository_hygiene",
    "dependency_management",
    "code_quality",
    "testing",
    "ci",
    "security",
    "configuration",
    "collaboration",
    "build_release",
]

AREA_STATUSES = ["pending", "in_progress", "examined", "not_applicable"]

MAX_NOTES = 25
MAX_NOTE_CHARS = 200


@dataclass
class Candidate:
    id: str
    data: dict  # report fields plus internal fields (kind, absent_globs, confidence)
    step: int
    retracted: bool = False
    retract_reason: str = ""


@dataclass
class AuditState:
    repo: Repo
    coverage: dict[str, str] = field(default_factory=lambda: dict.fromkeys(AREAS, "pending"))
    coverage_reasons: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    seen_lines: dict[str, set[int]] = field(default_factory=dict)
    finished: bool = False
    finish_summary: str = ""

    # --- seen lines -------------------------------------------------------
    def mark_seen(self, path: str, lines) -> None:
        self.seen_lines.setdefault(path, set()).update(lines)

    def was_seen(self, path: str, line: int) -> bool:
        return line in self.seen_lines.get(path, ())

    # --- candidates -------------------------------------------------------
    def active_candidates(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.retracted]

    def get_candidate(self, cid: str) -> Candidate | None:
        cid = cid.strip().upper()
        return next((c for c in self.candidates if c.id == cid), None)

    def add_candidate(self, data: dict, step: int) -> Candidate:
        c = Candidate(id=f"V{len(self.candidates) + 1}", data=data, step=step)
        self.candidates.append(c)
        return c

    # --- notes ------------------------------------------------------------
    def add_note(self, text: str) -> str:
        text = " ".join(text.split())[:MAX_NOTE_CHARS]
        if not text:
            return "ERROR: the note is empty."
        if text in self.notes:
            return "Note already recorded."
        self.notes.append(text)
        dropped = ""
        if len(self.notes) > MAX_NOTES:
            dropped = f" The oldest note was removed (limit {MAX_NOTES})."
            self.notes.pop(0)
        return f"Note recorded ({len(self.notes)}/{MAX_NOTES}).{dropped}"

    def pending_areas(self) -> list[str]:
        return [a for a, s in self.coverage.items() if s in ("pending", "in_progress")]
