"""Inspection tools.

Each tool reads the cloned copy only. No tool writes to it or runs code from it
(``run_tests`` is the exception and runs only in Docker with ``--allow-exec``).
Each result is redacted and cut to ``MAX_RESULT_CHARS`` before the LLM sees it.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import jsonschema
import regex
import yaml

from auditor.redact import redact
from auditor.repo import MAX_TEXT_BYTES, run_git
from auditor.rules import CATEGORIES
from auditor.state import AREA_STATUSES, AREAS, AuditState

MAX_RESULT_CHARS = 6000
READ_MAX_LINES = 200
LINE_TEXT_LIMIT = 200
VENDORED = {
    "node_modules",
    "venv",
    ".venv",
    "env",
    "dist",
    "build",
    "site-packages",
    "vendor",
    "__pycache__",
    ".tox",
    "target",
    ".mypy_cache",
    ".pytest_cache",
}


class ToolError(Exception):
    """A tool failed. The message goes to the LLM; the loop continues."""


@dataclass
class ToolContext:
    state: AuditState
    allow_exec: bool = False
    step: int = 0

    @property
    def repo(self):
        return self.state.repo

    @property
    def root(self) -> Path:
        return self.state.repo.root


# ---------------------------------------------------------------- helpers ---


def clip(text: str, limit: int = MAX_RESULT_CHARS, hint: str = "") -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    more = f" {hint}" if hint else ""
    return f"{cut}\n[... output cut at {limit} characters.{more}]"


def safe_path(ctx: ToolContext, rel: str, must_exist: bool = True) -> tuple[str, Path]:
    """Resolve a repository-relative path. Reject paths outside the root and inside .git."""
    rel = (rel or ".").strip().replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ToolError(f"Path must be relative to the repository root: {rel!r}")
    norm = str(PurePosixPath(rel))
    parts = PurePosixPath(norm).parts
    if ".." in parts:
        raise ToolError(f"Path is outside the repository: {rel!r}")
    if parts and parts[0] == ".git":
        raise ToolError("The .git directory is not available. Use git_log.")
    full = (ctx.root / norm).resolve()
    if full != ctx.root and not full.is_relative_to(ctx.root):
        raise ToolError(f"Path is outside the repository: {rel!r}")
    if must_exist and not full.exists():
        raise ToolError(f"No such file or directory: {norm!r}. Use list_files to see the files.")
    return ("." if norm == "." else norm), full


def glob_match(path: str, pattern: str) -> bool:
    pattern = pattern.strip().lstrip("./") if pattern.startswith("./") else pattern.strip()
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
        return True
    if "/" not in pattern and fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern):
        return True
    return False


def is_vendored(path: str) -> bool:
    return any(part in VENDORED for part in path.split("/")[:-1])


def read_lines(ctx: ToolContext, rel: str) -> list[str]:
    info = ctx.repo.files.get(rel)
    if info is None:
        raise ToolError(f"{rel!r} is not a tracked file. Use list_files to see the files.")
    if info.binary:
        raise ToolError(f"{rel!r} is binary or larger than {MAX_TEXT_BYTES} bytes.")
    text = (ctx.root / rel).read_bytes().decode("utf-8", "replace")
    return text.splitlines()


def human_size(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n >= 1024 else f"{n} B"


# ------------------------------------------------------- investigation ---


def list_files(ctx: ToolContext, path: str = ".", depth: int = 2, pattern: str | None = None) -> str:
    rel, full = safe_path(ctx, path)
    depth = max(1, min(int(depth), 6))
    if full.is_file():
        rel_files = [rel]
        prefix = ""
    else:
        prefix = "" if rel == "." else rel.rstrip("/") + "/"
        rel_files = [p for p in ctx.repo.files if p.startswith(prefix)]
    if pattern:
        rel_files = [p for p in rel_files if glob_match(p, pattern)]
    if not rel_files:
        return f"No tracked files under {rel!r}" + (f" that match {pattern!r}." if pattern else ".")

    files_out: list[str] = []
    dirs: Counter = Counter()
    for p in sorted(rel_files):
        sub = p[len(prefix) :]
        parts = sub.split("/")
        vend = next((i for i, part in enumerate(parts[:-1]) if part in VENDORED), None)
        if vend is not None and vend < depth:
            dirs["/".join(parts[: vend + 1]) + "/"] += 1
        elif len(parts) > depth:
            dirs["/".join(parts[:depth]) + "/"] += 1
        else:
            files_out.append(p)

    lines = []
    for p in files_out:
        info = ctx.repo.files[p]
        lc = "binary" if info.binary else f"{info.lines} lines"
        lines.append(f"{p}  ({human_size(info.size)}, {lc})")
    for d, n in sorted(dirs.items()):
        lines.append(f"{prefix}{d}  ({n} files below)")
    total = len(lines)
    if total > 300:
        lines = lines[:300] + [f"[... {total - 300} more entries. Use path or pattern to narrow.]"]
    header = f"{len(rel_files)} files under {rel!r} (depth {depth})"
    return clip(header + "\n" + "\n".join(lines))


def read_file(ctx: ToolContext, path: str, start_line: int = 1, end_line: int | None = None) -> str:
    rel, full = safe_path(ctx, path)
    if full.is_dir():
        raise ToolError(f"{rel!r} is a directory. Use list_files.")
    lines = read_lines(ctx, rel)
    total = len(lines)
    if total == 0:
        return f"{rel}: the file is empty (0 lines)."
    start = max(1, int(start_line))
    if start > total:
        raise ToolError(f"start_line {start} is after the end of {rel} ({total} lines).")
    end = total if end_line is None else max(start, min(int(end_line), total))
    end = min(end, start + READ_MAX_LINES - 1)

    width = len(str(end))
    out, used, shown_end = [], 0, start - 1
    for n in range(start, end + 1):
        text = redact(lines[n - 1], path=rel)
        if len(text) > 500:
            text = text[:500] + " [... line cut]"
        row = f"{n:>{width}}  {text}"
        if used + len(row) > MAX_RESULT_CHARS - 200 and out:
            break
        out.append(row)
        used += len(row) + 1
        shown_end = n
    ctx.state.mark_seen(rel, range(start, shown_end + 1))
    header = f"{rel}  lines {start}-{shown_end} of {total}"
    footer = ""
    if shown_end < total:
        footer = f"\n[More lines: call read_file with start_line={shown_end + 1}.]"
    return header + "\n" + "\n".join(out) + footer


def search_code(
    ctx: ToolContext,
    pattern: str,
    path_glob: str | None = None,
    ignore_case: bool = False,
    max_results: int = 40,
) -> str:
    if not pattern or len(pattern) > 200:
        raise ToolError("pattern must have 1 to 200 characters.")
    try:
        rx = regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)
    except regex.error as e:
        raise ToolError(f"Invalid regular expression: {e}") from None
    max_results = max(1, min(int(max_results), 100))
    deadline = time.monotonic() + 20
    hits, total, files_with_hits, timeouts = [], 0, set(), 0
    want_vendored = bool(path_glob) and any(v in path_glob for v in VENDORED)
    for rel in sorted(ctx.repo.files):
        info = ctx.repo.files[rel]
        if info.binary or (path_glob and not glob_match(rel, path_glob)):
            continue
        if is_vendored(rel) and not want_vendored:
            continue
        if time.monotonic() > deadline:
            hits.append("[Search stopped after 20 s. Use path_glob to narrow it.]")
            break
        try:
            lines = (ctx.root / rel).read_bytes().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            try:
                m = rx.search(line, timeout=0.2)
            except TimeoutError:
                timeouts += 1
                continue
            if not m:
                continue
            total += 1
            files_with_hits.add(rel)
            if len(hits) < max_results:
                text = redact(line.strip(), path=rel)[:LINE_TEXT_LIMIT]
                hits.append(f"{rel}:{n}: {text}")
                ctx.state.mark_seen(rel, [n])
    if total == 0:
        scope = f" in files matching {path_glob!r}" if path_glob else ""
        return f"No matches for {pattern!r}{scope}." + (f" ({timeouts} lines timed out.)" if timeouts else "")
    summary = f"{total} matches in {len(files_with_hits)} files"
    if total > max_results:
        summary += f" (first {max_results} shown)"
    if timeouts:
        summary += f"; {timeouts} lines timed out"
    return clip("\n".join(hits) + "\n" + summary + ".")


def git_log(
    ctx: ToolContext,
    max_count: int = 20,
    path: str | None = None,
    search: str | None = None,
    show_patch: bool = False,
) -> str:
    max_count = max(1, min(int(max_count), 100))
    args = [
        "log",
        "--no-color",
        "--date=short",
        f"--max-count={max_count}",
        "--format=commit %h  %ad  %an%n    %s",
    ]
    if show_patch:
        args += ["-p", "--unified=1", "--stat=100"]
    else:
        args += ["--name-status"]
    if search:
        if len(search) > 200:
            raise ToolError("search must have at most 200 characters.")
        args.append(f"-S{search}")
    if path:
        rel, _ = safe_path(ctx, path, must_exist=False)
        args += ["--", rel]
    try:
        out = run_git(args, ctx.root, timeout=30)
    except subprocess.TimeoutExpired:
        raise ToolError("git log timed out. Use a smaller max_count or a path.") from None
    except RuntimeError as e:
        raise ToolError(f"git log failed: {e}") from None
    if not out.strip():
        return "No commits match."
    out = "\n".join(redact(line) for line in out.splitlines())
    return clip(out, hint="Use path, search or a smaller max_count.")


# ------------------------------------------------------------ analysis ---


class _LineLoader(yaml.SafeLoader):
    """SafeLoader that keeps line numbers and does not turn 'on' into True."""


def _compose(text: str):
    return yaml.compose(text, Loader=_LineLoader)


def _scalar(node) -> str:
    if isinstance(node, yaml.ScalarNode):
        return str(node.value)
    if isinstance(node, yaml.SequenceNode):
        return "[" + ", ".join(_scalar(n) for n in node.value) + "]"
    if isinstance(node, yaml.MappingNode):
        return "{" + ", ".join(f"{_scalar(k)}: {_scalar(v)}" for k, v in node.value) + "}"
    return ""


def _get(node, key):
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == key:
                return k, v
    return None, None


def _line(node) -> int:
    return node.start_mark.line + 1


def _ref_type(uses: str) -> str:
    if uses.startswith("./"):
        return "local"
    if uses.startswith("docker://"):
        return "docker" + (" (digest)" if "@sha256:" in uses else " (tag)")
    if "@" not in uses:
        return "no ref"
    ref = uses.rsplit("@", 1)[1]
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return "sha"
    if re.fullmatch(r"v?\d+(\.\d+)*", ref):
        return "tag"
    return "branch or other"


_EXPR_RISK = re.compile(
    r"\$\{\{\s*(github\.event\.[A-Za-z0-9_.\[\]'*-]+|github\.head_ref|inputs\.[A-Za-z0-9_-]+)\s*\}\}"
)
_SECRET_USE = re.compile(r"secrets\.([A-Za-z0-9_]+)")


def _describe_workflow(rel: str, text: str, seen: set[int]) -> list[str]:
    out = [rel]

    def add(msg: str, node=None, line: int | None = None):
        ln = line or (_line(node) if node is not None else None)
        if ln:
            seen.add(ln)
            out.append(f"{msg}  [line {ln}]")
        else:
            out.append(msg)

    try:
        root = _compose(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        add(f"  YAML parse error: {getattr(e, 'problem', e)}", line=(mark.line + 1) if mark else None)
        return out
    if not isinstance(root, yaml.MappingNode):
        add("  Not a mapping. This is not a valid workflow.")
        return out

    k, on = _get(root, "on")
    if on is None:
        k, on = _get(root, "true")
    if on is None:
        add("  triggers: (none found)")
    elif isinstance(on, yaml.ScalarNode):
        add(f"  triggers: {on.value}", on)
    elif isinstance(on, yaml.SequenceNode):
        add(f"  triggers: {_scalar(on)}", on)
    else:
        for tk, tv in on.value:
            detail = "" if isinstance(tv, yaml.ScalarNode) and not tv.value else f" {_scalar(tv)}"
            add(f"  trigger: {tk.value}{detail[:200]}", tk)

    pk, perms = _get(root, "permissions")
    if perms is None:
        add("  top-level permissions: (not set)")
    else:
        add(f"  top-level permissions: {_scalar(perms)}", pk)

    ck, conc = _get(root, "concurrency")
    if conc is not None:
        add(f"  concurrency: {_scalar(conc)[:120]}", ck)

    _, jobs = _get(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        add("  jobs: (none found)")
        return out
    for jk, job in jobs.value:
        add(f"  job {jk.value!r}", jk)
        for key in (
            "runs-on",
            "permissions",
            "if",
            "needs",
            "continue-on-error",
            "environment",
            "uses",
            "timeout-minutes",
        ):
            k2, v2 = _get(job, key)
            if v2 is not None:
                add(f"    {key}: {_scalar(v2)[:200]}", k2)
        if _get(job, "permissions")[1] is None:
            out.append("    permissions: (not set)")
        _, steps = _get(job, "steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue
        for i, step in enumerate(steps.value, 1):
            _, uses = _get(step, "uses")
            _, run = _get(step, "run")
            _, name = _get(step, "name")
            label = f" ({_scalar(name)[:60]})" if name is not None else ""
            if uses is not None:
                add(f"    step {i}{label} uses {uses.value}  ref: {_ref_type(uses.value)}", uses)
                _, with_ = _get(step, "with")
                if isinstance(with_, yaml.MappingNode):
                    for wk, wv in with_.value:
                        add(f"      with {wk.value}: {_scalar(wv)[:150]}", wk)
            elif run is not None:
                cmd = str(run.value).strip().splitlines()
                first = " ; ".join(c.strip() for c in cmd[:4])[:240]
                more = f" (+{len(cmd) - 4} lines)" if len(cmd) > 4 else ""
                add(f"    step {i}{label} run: {first}{more}", run)
            else:
                add(f"    step {i}{label}", step)
            for key in ("if", "continue-on-error", "shell", "working-directory"):
                k3, v3 = _get(step, key)
                if v3 is not None:
                    add(f"      {key}: {_scalar(v3)[:150]}", k3)
            k4, env = _get(step, "env")
            if isinstance(env, yaml.MappingNode):
                for ek, ev in env.value:
                    add(f"      env {ek.value}: {_scalar(ev)[:120]}", ek)

    lines = text.splitlines()
    secrets = [(n, m.group(1)) for n, line in enumerate(lines, 1) for m in _SECRET_USE.finditer(line)]
    if secrets:
        add("  secrets used: " + ", ".join(f"{s} (line {n})" for n, s in secrets[:15]))
        seen.update(n for n, _ in secrets)
    risky = [(n, m.group(0)) for n, line in enumerate(lines, 1) for m in _EXPR_RISK.finditer(line)]
    if risky:
        add("  untrusted-input expressions: " + ", ".join(f"{e} (line {n})" for n, e in risky[:10]))
        seen.update(n for n, _ in risky)
    return out


def inspect_workflows(ctx: ToolContext, path: str | None = None) -> str:
    if path:
        rel, _ = safe_path(ctx, path)
        targets = [rel]
    else:
        targets = [
            p for p in ctx.repo.files if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))
        ]
    if not targets:
        return "No GitHub Actions workflow files in .github/workflows/."
    out = []
    for rel in sorted(targets):
        text = "\n".join(read_lines(ctx, rel))
        seen: set[int] = set()
        out.extend(redact(line) for line in _describe_workflow(rel, text, seen))
        ctx.state.mark_seen(rel, seen)
        out.append("")
    return clip("\n".join(out), hint="Call inspect_workflows with one path.")


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")


def classify_python(spec: str) -> str:
    spec = spec.split(";", 1)[0].strip()
    if not spec:
        return "none"
    if spec.startswith("@") or "://" in spec or spec.startswith(("./", "../", "/")):
        return "url_or_path"
    if spec.startswith("===") or (spec.startswith("==") and "*" not in spec and "," not in spec):
        return "exact"
    has_lower = bool(re.search(r"(>=|>|~=|==\s*[\d.]+\.\*)", spec))
    has_upper = bool(re.search(r"(<=|<(?!=)|~=|==\s*[\d.]+\.\*)", spec))
    if has_lower and has_upper:
        return "range"
    if has_lower:
        return "lower_only"
    if has_upper:
        return "upper_only"
    return "none"


def classify_poetry(spec) -> str:
    if isinstance(spec, dict):
        if any(k in spec for k in ("git", "path", "url")):
            return "url_or_path"
        spec = spec.get("version", "")
    spec = str(spec).strip()
    if spec in ("", "*"):
        return "none"
    if spec.startswith(("^", "~")) and not spec.startswith("~="):
        return "range"
    if re.fullmatch(r"\d+(\.\d+)*", spec):
        return "exact"
    return classify_python(spec)


def classify_npm(spec: str) -> str:
    spec = str(spec).strip()
    if spec in ("", "*", "latest", "x"):
        return "none"
    if re.match(r"^(git|file|link|http|github:|workspace:)", spec) or "/" in spec:
        return "url_or_path"
    if spec.startswith(("^", "~")):
        return "range"
    if re.fullmatch(r"=?v?\d+\.\d+\.\d+(-[\w.]+)?", spec):
        return "exact"
    if spec.startswith(">") and "<" not in spec:
        return "lower_only"
    return "range"


def _find_line(lines: list[str], needle: str) -> int | None:
    pat = re.compile(r"(^|[\"'\s])" + re.escape(needle) + r"([\"'\s=<>~!^\[:;,]|$)", re.I)
    for n, line in enumerate(lines, 1):
        if pat.search(line):
            return n
    return None


def _parse_requirements(ctx, rel, deps, seen, depth=0):
    if depth > 3:
        return
    for n, raw in enumerate(read_lines(ctx, rel), 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r", "--requirement", "-c", "--constraint")):
            inc = line.split(None, 1)[1] if " " in line else line[2:]
            inc_rel = str(PurePosixPath(rel).parent / inc.strip())
            inc_rel = inc_rel[2:] if inc_rel.startswith("./") else inc_rel
            if inc_rel in ctx.repo.files:
                _parse_requirements(ctx, inc_rel, deps, seen, depth + 1)
            continue
        if line.startswith(("-e", "--editable", "git+", "http")) or "://" in line:
            deps.append((rel, n, line[:60], "url_or_path", line))
            seen.setdefault(rel, set()).add(n)
            continue
        if line.startswith("-"):
            continue
        m = _REQ_NAME.match(line)
        if m:
            deps.append((rel, n, m.group(1), classify_python(m.group(3)), m.group(3).strip()))
            seen.setdefault(rel, set()).add(n)


def _pep508(item: str) -> tuple[str, str]:
    m = _REQ_NAME.match(item)
    return (m.group(1), m.group(3).strip()) if m else (item, "")


def inspect_dependencies(ctx: ToolContext) -> str:
    deps: list[tuple[str, int | None, str, str, str]] = []  # file, line, name, type, spec
    seen: dict[str, set[int]] = {}
    notes: list[str] = []
    files = ctx.repo.files

    for rel in sorted(files):
        if is_vendored(rel) or files[rel].binary:
            continue
        base = rel.rsplit("/", 1)[-1]
        try:
            if re.fullmatch(r"requirements.*\.(txt|in)", base):
                _parse_requirements(ctx, rel, deps, seen)
            elif base == "pyproject.toml":
                lines = read_lines(ctx, rel)
                data = tomllib.loads("\n".join(lines))
                proj = data.get("project", {})
                items = list(proj.get("dependencies", []))
                for group in proj.get("optional-dependencies", {}).values():
                    items += group
                for group in data.get("dependency-groups", {}).values():
                    items += [g for g in group if isinstance(g, str)]
                for item in items:
                    name, spec = _pep508(item)
                    ln = _find_line(lines, name)
                    deps.append((rel, ln, name, classify_python(spec), spec))
                poetry = data.get("tool", {}).get("poetry", {})
                sections = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
                sections += [g.get("dependencies", {}) for g in poetry.get("group", {}).values()]
                for sec in sections:
                    for name, spec in sec.items():
                        if name.lower() == "python":
                            continue
                        ln = _find_line(lines, name)
                        deps.append((rel, ln, name, classify_poetry(spec), str(spec)[:40]))
                if "project" in data and "requires-python" not in proj:
                    notes.append(f"{rel}: [project] has no requires-python.")
            elif base == "setup.cfg":
                lines = read_lines(ctx, rel)
                block = re.search(r"install_requires\s*=\s*\n((?:[ \t]+.+\n?)+)", "\n".join(lines) + "\n")
                if block:
                    for item in block.group(1).split("\n"):
                        item = item.strip()
                        if item:
                            name, spec = _pep508(item)
                            deps.append((rel, _find_line(lines, name), name, classify_python(spec), spec))
            elif base == "Pipfile":
                lines = read_lines(ctx, rel)
                data = tomllib.loads("\n".join(lines))
                for sec in ("packages", "dev-packages"):
                    for name, spec in data.get(sec, {}).items():
                        deps.append(
                            (rel, _find_line(lines, name), name, classify_poetry(spec), str(spec)[:40])
                        )
            elif base == "package.json":
                lines = read_lines(ctx, rel)
                data = json.loads("\n".join(lines))
                for sec in ("dependencies", "devDependencies"):
                    for name, spec in (data.get(sec) or {}).items():
                        deps.append((rel, _find_line(lines, name), name, classify_npm(spec), str(spec)[:40]))
        except (tomllib.TOMLDecodeError, json.JSONDecodeError, ValueError) as e:
            notes.append(f"{rel}: parse error: {str(e)[:120]}")
        except ToolError as e:
            notes.append(f"{rel}: {e}")

    lock_names = (
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "npm-shrinkwrap.json",
    )
    locks = [p for p in files if p.rsplit("/", 1)[-1] in lock_names and not is_vendored(p)]
    locks += [p for p in files if re.search(r"requirements.*\.lock$", p)]

    if not deps and not locks:
        return (
            "No dependency manifests found (requirements*.txt, pyproject.toml, setup.cfg, "
            "Pipfile, package.json)." + ("\n" + "\n".join(notes) if notes else "")
        )

    out = []
    by_file: dict[str, list] = {}
    for d in deps:
        by_file.setdefault(d[0], []).append(d)
    for rel, items in by_file.items():
        counts = Counter(d[3] for d in items)
        out.append(f"{rel}: {len(items)} deps; " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
        for _, ln, name, kind, spec in items[:40]:
            loc = f"line {ln}" if ln else "line ?"
            out.append(f"  {loc}: {name} {spec or '(no version)'}  -> {kind}")
            if ln:
                seen.setdefault(rel, set()).add(ln)
        if len(items) > 40:
            out.append(f"  [... {len(items) - 40} more]")
    out.append("Lockfiles present: " + (", ".join(sorted(locks)) or "(none)"))

    specs: dict[str, set[tuple[str, str]]] = {}
    for rel, _, name, _, spec in deps:
        specs.setdefault(re.sub(r"[-_.]+", "-", name.lower()), set()).add((rel, spec))
    diffs = [(n, s) for n, s in specs.items() if len({r for r, _ in s}) > 1 and len({sp for _, sp in s}) > 1]
    if diffs:
        out.append("Packages with different constraints in different files:")
        for n, s in diffs[:15]:
            out.append("  " + n + ": " + "; ".join(f"{r} '{sp}'" for r, sp in sorted(s)))
    out += notes
    for rel, lns in seen.items():
        ctx.state.mark_seen(rel, lns)
    return clip("\n".join(redact(x) for x in out))


COMMON_LOCAL_PATTERNS = [
    "*.pyc",
    "*.pyo",
    "__pycache__/*",
    ".DS_Store",
    "Thumbs.db",
    "*.log",
    ".env",
    ".env.*",
    "*.env",
    ".idea/*",
    ".vscode/settings.json",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.swp",
    "*~",
    ".coverage",
    "htmlcov/*",
    ".pytest_cache/*",
    "*.egg-info/*",
    "dist/*",
    "build/*",
    "node_modules/*",
    ".venv/*",
    "venv/*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    ".ipynb_checkpoints/*",
    "*.tmp",
    "*.bak",
    "npm-debug.log*",
    "terraform.tfstate*",
    ".terraform/*",
    "*.tfvars",
]


def _collapse(paths: list[str], limit: int = 40) -> list[str]:
    if len(paths) <= limit:
        return paths
    groups = Counter(p.split("/", 1)[0] + ("/" if "/" in p else "") for p in paths)
    return [f"{g} ({n} files)" if n > 1 else g for g, n in groups.most_common(limit)]


def git_tracked_ignored(ctx: ToolContext) -> str:
    try:
        ignored = run_git(["ls-files", "-c", "-i", "--exclude-standard", "-z"], ctx.root)
    except RuntimeError as e:
        raise ToolError(f"git ls-files failed: {e}") from None
    ignored_list = sorted(filter(None, ignored.split("\0")))
    common = []
    for p in sorted(ctx.repo.files):
        base = p.rsplit("/", 1)[-1]
        if re.search(r"(example|sample|template|dist|default)", base, re.I):
            continue
        for pat in COMMON_LOCAL_PATTERNS:
            if pat.endswith("/*"):
                d = pat[:-2]
                if f"/{d}/" in f"/{p}" or (fnmatch.fnmatch(p.split("/", 1)[0], d) and "/" in p):
                    common.append((p, pat))
                    break
            elif fnmatch.fnmatch(base, pat):
                common.append((p, pat))
                break
    gi = ".gitignore present" if ".gitignore" in ctx.repo.files else "NO root .gitignore"
    out = [f"Root .gitignore: {gi}."]
    out.append("Tracked files that match .gitignore rules (they were committed before or forced):")
    out += ["  " + p for p in _collapse(ignored_list)]
    if not ignored_list:
        out.append("  (none)")
    out.append("Tracked files that match common local/temporary/secret patterns:")
    if common:
        paths = [p for p, _ in common]
        if len(paths) > 40:
            out += ["  " + p for p in _collapse(paths)]
        else:
            out += [f"  {p}  (pattern {pat})" for p, pat in common]
    else:
        out.append("  (none)")
    out.append("Note: example/template files such as .env.example are normal.")
    return clip("\n".join(out))


def run_ruff(ctx: ToolContext, path: str = ".") -> str:
    rel, _ = safe_path(ctx, path)
    py = [
        p
        for p in ctx.repo.files
        if p.endswith(".py")
        and not is_vendored(p)
        and (rel == "." or p == rel or p.startswith(rel.rstrip("/") + "/"))
    ]
    if not py:
        return "No Python files in this path. ruff was not run."
    cfg = [p for p in ("ruff.toml", ".ruff.toml") if p in ctx.repo.files]
    if "pyproject.toml" in ctx.repo.files:
        try:
            if "ruff" in tomllib.loads((ctx.root / "pyproject.toml").read_text("utf-8", "replace")).get(
                "tool", {}
            ):
                cfg.append("pyproject.toml [tool.ruff]")
        except (tomllib.TOMLDecodeError, OSError):
            pass
    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--no-fix",
        "--no-cache",
        "--exit-zero",
        "--output-format",
        "json",
        "--force-exclude",
        rel,
    ]
    try:
        proc = subprocess.run(cmd, cwd=ctx.root, capture_output=True, timeout=90, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolError("ruff timed out after 90 s.") from None
    except FileNotFoundError:
        raise ToolError("ruff is not available. Do not report lint results.") from None
    try:
        issues = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
    except json.JSONDecodeError:
        err = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise ToolError(f"ruff did not give JSON output. {err}") from None
    counts = Counter(i.get("code") or "syntax" for i in issues)
    out = [
        f"ruff checked {len(py)} Python files. Repository ruff config: "
        + (", ".join(cfg) if cfg else "none (ruff default rules used)")
        + "."
    ]
    out.append(
        f"{len(issues)} issues."
        + (" By rule: " + ", ".join(f"{c} {n}" for c, n in counts.most_common(15)) if issues else "")
    )
    for i in issues[:20]:
        f = os.path.relpath(i.get("filename", ""), ctx.root).replace("\\", "/")
        row = (i.get("location") or {}).get("row")
        out.append(f"  {f}:{row}: {i.get('code') or 'syntax'} {i.get('message', '')[:120]}")
        if row and f in ctx.repo.files:
            ctx.state.mark_seen(f, [row])
    out.append("Note: ruff output alone is not a violation. Report only clear maintainability problems.")
    return clip("\n".join(redact(x) for x in out))


def run_tests(ctx: ToolContext, timeout_s: int = 180) -> str:
    """Run pytest in Docker: install deps with network, then run tests without network."""
    if not ctx.allow_exec:
        return "Test execution is not available (run without --allow-exec)."
    if not shutil.which("docker"):
        return "Test execution is not available (Docker not found). Do not report test failures."
    image = os.environ.get("TEST_IMAGE", "python:3.12-slim")
    vol = f"devops-audit-deps-{os.getpid()}-{int(time.time())}"
    src = str(ctx.root)
    base = [
        "docker",
        "run",
        "--rm",
        "--memory",
        "1g",
        "--cpus",
        "2",
        "--pids-limit",
        "256",
        "-v",
        f"{src}:/repo:ro",
        "-v",
        f"{vol}:/deps",
    ]
    install = (
        "cp -r /repo /work && cd /work && pip install -q --target /deps pytest "
        "&& (if [ -f requirements.txt ]; then pip install -q --target /deps -r requirements.txt; "
        "elif [ -f pyproject.toml ] || [ -f setup.py ]; then pip install -q --target /deps .; fi)"
    )
    test = (
        "cp -r /repo /work && cd /work && PYTHONPATH=/deps:/work python -m pytest -q "
        "-p no:cacheprovider --tb=line 2>&1 | tail -n 40"
    )
    try:
        p1 = subprocess.run(base + [image, "sh", "-c", install], capture_output=True, timeout=timeout_s)
        if p1.returncode != 0:
            return (
                "Test execution is not available: dependency install failed in Docker. "
                "Do not report test failures. " + redact(p1.stderr.decode("utf-8", "replace")[-400:])
            )
        p2 = subprocess.run(
            base + ["--network", "none", image, "sh", "-c", test], capture_output=True, timeout=timeout_s
        )
        out = p2.stdout.decode("utf-8", "replace")
        return clip("pytest output (last 40 lines, no network):\n" + redact(out))
    except subprocess.TimeoutExpired:
        return "Test execution timed out. Do not report test failures from this."
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True, timeout=60)


# --------------------------------------------------------- state tools ---


def add_note(ctx: ToolContext, text: str) -> str:
    return ctx.state.add_note(redact(text))


def update_coverage(ctx: ToolContext, area: str, status: str, reason: str = "") -> str:
    area = area.strip().lower()
    if area not in AREAS:
        raise ToolError(f"area must be one of {AREAS}.")
    if status not in AREA_STATUSES[1:]:
        raise ToolError(f"status must be one of {AREA_STATUSES[1:]}.")
    ctx.state.coverage[area] = status
    ctx.state.coverage_reasons[area] = " ".join(redact(reason).split())[:150]
    left = ctx.state.pending_areas()
    return f"{area} -> {status}. Areas not done: {', '.join(left) if left else 'none'}."


def record_violation(ctx: ToolContext, **kwargs) -> str:
    from auditor.report import validate_candidate

    clean, errors, warnings = validate_candidate(ctx.state, kwargs)
    if errors:
        return "REJECTED:\n- " + "\n- ".join(errors)
    c = ctx.state.add_candidate(clean, ctx.step)
    loc = f"{clean['file']}:{clean['line']}" if clean["line"] else (clean["file"] or "(no file)")
    msg = f"Recorded {c.id}: {clean['rule_id']} {clean['severity']} {loc}."
    if warnings:
        msg += "\nWARNING: " + " ".join(warnings)
    return msg


def update_violation(ctx: ToolContext, id: str, **changes) -> str:
    from auditor.report import validate_candidate

    c = ctx.state.get_candidate(id)
    if c is None or c.retracted:
        raise ToolError(f"No active violation {id!r}.")
    merged = {**c.data, **changes}
    clean, errors, warnings = validate_candidate(ctx.state, merged, ignore_id=c.id)
    if errors:
        return "REJECTED:\n- " + "\n- ".join(errors)
    c.data = clean
    return f"Updated {c.id}." + ("\nWARNING: " + " ".join(warnings) if warnings else "")


def retract_violation(ctx: ToolContext, id: str, reason: str) -> str:
    c = ctx.state.get_candidate(id)
    if c is None or c.retracted:
        raise ToolError(f"No active violation {id!r}.")
    c.retracted, c.retract_reason = True, " ".join(reason.split())[:200]
    return f"Retracted {c.id}. Active violations: {len(ctx.state.active_candidates())}."


def finish(ctx: ToolContext, summary: str = "") -> str:
    ctx.state.finished = True
    ctx.state.finish_summary = " ".join(redact(summary).split())[:500]
    return "Finish requested."


# ------------------------------------------------------------ registry ---

_REASON = {"type": "string", "description": "One sentence: why you call this tool now."}
_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


# name -> (function, description, parameters schema, takes a "reason" argument)
TOOL_DEFS: dict[str, tuple] = {
    "list_files": (
        list_files,
        "List tracked files with size and line count. Deep or vendored directories show as one line.",
        _schema(
            {
                "path": _STR,
                "depth": {**_INT, "description": "1-6, default 2"},
                "pattern": {**_STR, "description": "glob, e.g. *.yml"},
                "reason": _REASON,
            },
            ["reason"],
        ),
        True,
    ),
    "read_file": (
        read_file,
        "Read a text file with line numbers (max 200 lines per call). Secrets show as <REDACTED:...>.",
        _schema({"path": _STR, "start_line": _INT, "end_line": _INT, "reason": _REASON}, ["path", "reason"]),
        True,
    ),
    "search_code": (
        search_code,
        "Regex search over tracked text files. Returns file:line: text. Skips vendored dirs unless "
        "path_glob names them.",
        _schema(
            {
                "pattern": _STR,
                "path_glob": _STR,
                "ignore_case": _BOOL,
                "max_results": _INT,
                "reason": _REASON,
            },
            ["pattern", "reason"],
        ),
        True,
    ),
    "git_log": (
        git_log,
        "Commit history: hash, date, author, subject, changed files. search=TEXT finds commits that "
        "add or remove TEXT (git log -S). show_patch=true shows diffs.",
        _schema(
            {"max_count": _INT, "path": _STR, "search": _STR, "show_patch": _BOOL, "reason": _REASON},
            ["reason"],
        ),
        True,
    ),
    "inspect_workflows": (
        inspect_workflows,
        "Facts about GitHub Actions workflows with line numbers: triggers, permissions, jobs, steps, "
        "action refs (sha/tag/branch), with/env values, secrets used, untrusted-input expressions.",
        _schema({"path": _STR, "reason": _REASON}, ["reason"]),
        True,
    ),
    "inspect_dependencies": (
        inspect_dependencies,
        "Parse dependency manifests: each dependency with its constraint type (exact, range, "
        "lower_only, none, url_or_path), lockfiles present, conflicting constraints.",
        _schema({"reason": _REASON}, ["reason"]),
        True,
    ),
    "git_tracked_ignored": (
        git_tracked_ignored,
        "Tracked files that match .gitignore, and tracked files that match common temporary, local "
        "or secret file patterns.",
        _schema({"reason": _REASON}, ["reason"]),
        True,
    ),
    "run_ruff": (
        run_ruff,
        "Run the ruff linter (static, does not execute code): issue counts by rule and the first "
        "issues. Tells if the repository has a ruff config.",
        _schema({"path": _STR, "reason": _REASON}, ["reason"]),
        True,
    ),
    "run_tests": (
        run_tests,
        "Run pytest in an isolated Docker container. Returns the summary.",
        _schema({"reason": _REASON}, ["reason"]),
        True,
    ),
    "add_note": (
        add_note,
        "Keep a short fact (max 200 chars). Old tool results leave your context; notes stay.",
        _schema({"text": _STR}, ["text"]),
        False,
    ),
    "update_coverage": (
        update_coverage,
        "Set the status of an inspection area: in_progress, examined or not_applicable.",
        _schema(
            {
                "area": {"type": "string", "enum": AREAS},
                "status": {"type": "string", "enum": ["in_progress", "examined", "not_applicable"]},
                "reason": _STR,
            },
            ["area", "status", "reason"],
        ),
        False,
    ),
    "record_violation": (
        record_violation,
        "Record one violation with direct evidence. Python validates it and can reject it.",
        _schema(
            {
                "rule_id": {**_STR, "description": "e.g. SEC-001; the prefix must match the category"},
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "file": {**_NULLABLE_STR, "description": "repo-relative path, or null"},
                "line": {**_NULLABLE_INT, "description": "a line you have seen, or null"},
                "description": {**_STR, "description": "max 160 chars"},
                "evidence": {**_STR, "description": "what the tool showed; never a secret value"},
                "recommendation": _STR,
                "kind": {"type": "string", "enum": ["present", "absent", "history"]},
                "absent_globs": {
                    "type": "array",
                    "items": _STR,
                    "description": "kind=absent only: file patterns that have no match",
                },
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            [
                "rule_id",
                "category",
                "severity",
                "file",
                "line",
                "description",
                "evidence",
                "recommendation",
                "kind",
                "confidence",
            ],
        ),
        False,
    ),
    "update_violation": (
        update_violation,
        "Change fields of a recorded violation (by id, e.g. V2).",
        _schema(
            {
                "id": _STR,
                "rule_id": _STR,
                "category": _STR,
                "severity": _STR,
                "file": _NULLABLE_STR,
                "line": _NULLABLE_INT,
                "description": _STR,
                "evidence": _STR,
                "recommendation": _STR,
                "confidence": _STR,
            },
            ["id"],
        ),
        False,
    ),
    "retract_violation": (
        retract_violation,
        "Remove a recorded violation that is weak, wrong or a duplicate.",
        _schema({"id": _STR, "reason": _STR}, ["id", "reason"]),
        False,
    ),
    "finish": (
        finish,
        "End the current phase when the audit is complete.",
        _schema({"summary": _STR}, ["summary"]),
        False,
    ),
}


def tool_names(allow_exec: bool) -> list[str]:
    return [n for n in TOOL_DEFS if allow_exec or n != "run_tests"]


def tool_schemas(names: list[str]) -> list[dict]:
    """Tool definitions in the OpenAI/Groq function-calling format."""
    return [
        {
            "type": "function",
            "function": {"name": n, "description": TOOL_DEFS[n][1], "parameters": TOOL_DEFS[n][2]},
        }
        for n in names
    ]


def _coerce(value, schema: dict):
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if isinstance(value, str):
        v = value.strip()
        if "null" in types and v.lower() in ("null", "none", ""):
            return None
        if "integer" in types and re.fullmatch(r"-?\d+", v):
            return int(v)
        if "boolean" in types and v.lower() in ("true", "false"):
            return v.lower() == "true"
    if "integer" in types and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def parse_arguments(name: str, raw) -> dict:
    """Parse and validate tool arguments. Unknown keys are dropped. Raise ToolError on failure."""
    if name not in TOOL_DEFS:
        raise ToolError(f"Unknown tool {name!r}. Available: {', '.join(TOOL_DEFS)}.")
    if isinstance(raw, str):
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            raise ToolError(f"Arguments are not valid JSON: {e.msg}.") from None
    else:
        args = raw or {}
    if not isinstance(args, dict):
        raise ToolError("Arguments must be a JSON object.")
    schema = TOOL_DEFS[name][2]
    props = schema["properties"]
    args = {k: _coerce(v, props[k]) for k, v in args.items() if k in props}
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as e:
        where = ".".join(str(p) for p in e.path) or "arguments"
        raise ToolError(f"Invalid arguments ({where}): {e.message}") from None
    return args


def execute(ctx: ToolContext, name: str, raw_args) -> tuple[str, dict, bool]:
    """Run a tool. Return (result text, parsed args, ok). Errors become text for the LLM."""
    try:
        args = parse_arguments(name, raw_args)
    except ToolError as e:
        return f"ERROR: {e}", {}, False
    if name == "run_tests" and not ctx.allow_exec:
        return "ERROR: run_tests is not available.", args, False
    func, _, _, takes_reason = TOOL_DEFS[name]
    call_args = {k: v for k, v in args.items() if not (takes_reason and k == "reason")}
    try:
        return func(ctx, **call_args), args, True
    except ToolError as e:
        return f"ERROR: {e}", args, False
    except subprocess.TimeoutExpired:
        return "ERROR: the tool timed out.", args, False
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as e:
        return f"ERROR: {type(e).__name__}: {str(e)[:200]}", args, False
