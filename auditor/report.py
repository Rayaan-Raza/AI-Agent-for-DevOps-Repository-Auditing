"""Violation validation, deduplication and the JSON report (Phase 3)."""

from __future__ import annotations

import json
import os
import re
import tempfile

import jsonschema

from auditor.redact import redact
from auditor.repo import run_git
from auditor.rules import CATEGORIES, PREFIX_TO_CATEGORY
from auditor.state import AuditState

SEVERITIES = ["critical", "high", "medium", "low"]
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
REPORT_FIELDS = [
    "rule_id",
    "category",
    "severity",
    "file",
    "line",
    "description",
    "evidence",
    "recommendation",
]
KINDS = ["present", "absent", "history"]
CONFIDENCES = ["high", "medium", "low"]
TEXT_LIMITS = {"description": 160, "evidence": 400, "recommendation": 400}

CATEGORY_ALIASES = {
    "hygiene": "repository_hygiene",
    "repo_hygiene": "repository_hygiene",
    "repository hygiene": "repository_hygiene",
    "dependencies": "dependency_management",
    "dependency management": "dependency_management",
    "quality": "code_quality",
    "code quality": "code_quality",
    "tests": "testing",
    "test": "testing",
    "github_actions": "ci",
    "github actions": "ci",
    "ci/cd": "ci",
    "github actions / ci": "ci",
    "cicd": "ci",
    "config": "configuration",
    "build": "build_release",
    "release": "build_release",
    "build and release": "build_release",
    "build_and_release": "build_release",
}

REPORT_SCHEMA = {
    "type": "object",
    "required": ["repository", "violations"],
    "additionalProperties": False,
    "properties": {
        "repository": {"type": "string", "minLength": 1},
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": REPORT_FIELDS,
                "additionalProperties": False,
                "properties": {
                    "rule_id": {"type": "string", "pattern": r"^[A-Z]{2,3}-\d{3}$"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "file": {"type": ["string", "null"], "minLength": 1},
                    "line": {"type": ["integer", "null"], "minimum": 1},
                    "description": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string", "minLength": 1},
                    "recommendation": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

_BRANCH_PROTECTION = re.compile(r"branch[\s_-]*protection|protected[\s_-]*branch|required[\s_-]*review", re.I)


def _none_like(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"))


def _norm_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _glob_hits(state: AuditState, pattern: str) -> list[str]:
    from tools import glob_match  # local import: tools imports this package

    return [p for p in state.repo.files if glob_match(p, pattern)]


def _in_history(state: AuditState, path: str) -> bool:
    try:
        return bool(run_git(["log", "--all", "--format=%h", "-1", "--", path], state.repo.root).strip())
    except Exception:
        return False


def validate_candidate(
    state: AuditState, raw: dict, ignore_id: str | None = None, check_duplicates: bool = True
) -> tuple[dict | None, list[str], list[str]]:
    """Return (clean data, errors, warnings). ``clean`` is None if there are errors."""
    errors: list[str] = []
    warnings: list[str] = []
    d = dict(raw)

    cat = str(d.get("category", "")).strip().lower()
    cat = CATEGORY_ALIASES.get(cat, cat).replace(" ", "_")
    if cat not in CATEGORIES:
        errors.append(f"category must be one of {list(CATEGORIES)}.")
    d["category"] = cat

    rid = str(d.get("rule_id", "")).strip().upper()
    if not re.fullmatch(r"[A-Z]{2,3}-\d{3}", rid):
        errors.append("rule_id must look like SEC-001 (prefix, dash, three digits).")
    elif cat in CATEGORIES and PREFIX_TO_CATEGORY.get(rid.split("-")[0]) != cat:
        errors.append(f"rule_id prefix for category {cat!r} must be {CATEGORIES[cat]}-.")
    d["rule_id"] = rid

    sev = str(d.get("severity", "")).strip().lower()
    if sev not in SEVERITIES:
        errors.append(f"severity must be one of {SEVERITIES}.")
    d["severity"] = sev

    kind = str(d.get("kind") or "present").strip().lower()
    if kind not in KINDS:
        errors.append(f"kind must be one of {KINDS}.")
    d["kind"] = kind

    conf = str(d.get("confidence") or "medium").strip().lower()
    d["confidence"] = conf if conf in CONFIDENCES else "medium"

    f = None if _none_like(d.get("file")) else _norm_path(str(d["file"]))
    line = d.get("line")
    if _none_like(line):
        line = None
    else:
        try:
            line = int(line)
        except (TypeError, ValueError):
            errors.append("line must be an integer or null.")
            line = None
    d["file"], d["line"] = f, line

    if line is not None and f is None:
        errors.append("line needs a file. Set line to null, or give the file.")
    if f is not None:
        if kind == "history":
            if f not in state.repo.files and not _in_history(state, f):
                errors.append(f"{f!r} is not in the repository or its git history.")
            if line is not None:
                errors.append("For kind 'history', set line to null and give the commit in evidence.")
        elif f not in state.repo.files:
            errors.append(f"{f!r} is not a tracked file. Use the exact path from list_files.")
        elif line is not None:
            info = state.repo.files[f]
            if info.lines is not None and not 1 <= line <= max(info.lines, 1):
                errors.append(
                    f"line {line} is not in {f} ({info.lines} lines). Read the file and "
                    "correct the line number."
                )
            elif not state.was_seen(f, line):
                errors.append(
                    f"You have not looked at {f} line {line}. Use read_file or search_code "
                    "to see the line first."
                )

    if kind == "absent":
        globs = d.get("absent_globs") or []
        if isinstance(globs, str):
            globs = [globs]
        if not globs:
            errors.append("kind 'absent' needs absent_globs, for example [\"README*\"].")
        for g in globs:
            hits = _glob_hits(state, str(g))
            if hits:
                errors.append(f"absent_globs {g!r} matches {hits[:3]}. The item is not absent.")
        d["absent_globs"] = list(globs)

    for key, limit in TEXT_LIMITS.items():
        val = d.get(key)
        if _none_like(val):
            errors.append(f"{key} is mandatory.")
            continue
        val = " ".join(str(val).split())
        red = redact(val)
        if red != val:
            warnings.append(f"A secret-like value in {key} was redacted. Never copy secret values.")
        if len(red) > limit:
            red = red[: limit - 3].rstrip() + "..."
        d[key] = red

    text = " ".join(str(d.get(k, "")) for k in ("description", "evidence", "recommendation"))
    if _BRANCH_PROTECTION.search(text) and not re.search(r"auto[\s_-]*merge", text, re.I):
        errors.append(
            "Do not report remote branch protection or required reviews. Files cannot show these settings."
        )

    if not errors and check_duplicates:
        for c in state.active_candidates():
            if c.id == ignore_id:
                continue
            o = c.data
            if (o["rule_id"], o["file"], o["line"]) == (rid, f, line):
                errors.append(f"Duplicate of {c.id}. Use update_violation to change {c.id}.")
                break

    return (None if errors else d), errors, warnings


def build_report(state: AuditState, repository: str, drop_low_confidence: bool = True) -> dict:
    """Validate, deduplicate and sort the active candidates. Return the report object."""
    kept: dict[tuple, dict] = {}
    for c in state.active_candidates():
        if drop_low_confidence and c.data.get("confidence") == "low":
            continue
        clean, errors, _ = validate_candidate(state, c.data, ignore_id=c.id, check_duplicates=False)
        if errors:
            continue
        key = (clean["rule_id"], clean["file"], clean["line"])
        old = kept.get(key)
        if old is None or SEVERITY_RANK[clean["severity"]] < SEVERITY_RANK[old["severity"]]:
            kept[key] = clean
    items = sorted(
        kept.values(),
        key=lambda v: (SEVERITY_RANK[v["severity"]], v["file"] or "", v["line"] or 0, v["rule_id"]),
    )
    report = {
        "repository": repository,
        "violations": [{k: v[k] for k in REPORT_FIELDS} for v in items],
    }
    jsonschema.validate(report, REPORT_SCHEMA)
    return report


def write_report(report: dict, path: str) -> None:
    """Write the JSON atomically: a temporary file in the same directory, then rename."""
    jsonschema.validate(report, REPORT_SCHEMA)
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".report-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
