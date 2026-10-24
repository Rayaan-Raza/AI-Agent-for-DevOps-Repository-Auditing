"""Repository setup: URL validation, safe clone, file index and profile (Phase 0)."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

GITHUB_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)

MAX_TEXT_BYTES = 1_000_000
CLONE_TIMEOUT = 180

# Names that the profile reports as present or absent. These are facts only.
KEY_FILES = {
    "readme": ["README*", "readme*"],
    "gitignore": [".gitignore"],
    "license": ["LICENSE*", "COPYING*"],
    "python_deps": [
        "requirements*.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "environment.yml",
    ],
    "lockfiles": [
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "requirements*.lock",
    ],
    "node": ["package.json"],
    "docker": ["Dockerfile*", "docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", ".dockerignore"],
    "github_workflows": [".github/workflows/*.yml", ".github/workflows/*.yaml"],
    "other_ci": [
        ".gitlab-ci.yml",
        ".circleci/config.yml",
        "Jenkinsfile",
        "azure-pipelines.yml",
        ".travis.yml",
    ],
    "lint_config": [
        ".pre-commit-config.yaml",
        "ruff.toml",
        ".ruff.toml",
        ".flake8",
        ".pylintrc",
        "tox.ini",
        "setup.cfg",
        ".editorconfig",
    ],
    "review_guidance": [
        "CONTRIBUTING*",
        ".github/CONTRIBUTING*",
        ".github/PULL_REQUEST_TEMPLATE*",
        ".github/pull_request_template*",
        "CODEOWNERS",
        ".github/CODEOWNERS",
        "docs/CODEOWNERS",
    ],
    "dependabot": [
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
        "renovate.json",
        ".github/renovate.json",
    ],
    "env_files": [".env", ".env.*", "*.env"],
    "makefile": ["Makefile", "justfile", "noxfile.py", "tasks.py"],
}


class RepoError(Exception):
    """Setup failure. ``exit_code`` is the process exit code to use."""

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class FileInfo:
    size: int
    lines: int | None  # None for binary or very large files
    binary: bool


@dataclass
class Repo:
    url: str
    root: Path
    files: dict[str, FileInfo] = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    _tmp: str | None = None

    def cleanup(self) -> None:
        if self._tmp and os.path.isdir(self._tmp):
            shutil.rmtree(self._tmp, onerror=_force_remove)
            self._tmp = None

    def glob(self, pattern: str) -> list[str]:
        """Files in the index that match a glob. Patterns without '/' match the basename too."""
        out = []
        for p in self.files:
            if fnmatch.fnmatch(p, pattern) or (
                "/" not in pattern and fnmatch.fnmatch(p.rsplit("/", 1)[-1], pattern)
            ):
                out.append(p)
        return sorted(out)


def _force_remove(func, path, _exc):
    # Git objects are read-only on Windows.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def normalize_url(url: str) -> str:
    m = GITHUB_URL.match(url.strip())
    if not m:
        raise RepoError(f"Invalid repository URL: {url!r}. Use https://github.com/<owner>/<repo>.", 2)
    return f"https://github.com/{m.group('owner')}/{m.group('repo')}"


def git_env() -> dict:
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
    )
    return env


def run_git(args: list[str], cwd: Path | str, timeout: int = 30) -> str:
    """Run a read-only git command. Raise RuntimeError on failure."""
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-c", "core.fsmonitor=false", *args],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "git failed")
    return proc.stdout.decode("utf-8", "replace")


def clone(source: str, allow_local: bool = False) -> Repo:
    """Clone a GitHub URL (or a local path when ``allow_local``) into a temporary directory."""
    local = allow_local and os.path.isdir(source)
    url = os.path.abspath(source) if local else normalize_url(source)
    tmp = tempfile.mkdtemp(prefix="devops-audit-")
    hooks = os.path.join(tmp, "no-hooks")
    os.makedirs(hooks)
    dest = os.path.join(tmp, "repo")
    cmd = [
        "git",
        "-c",
        f"core.hooksPath={hooks}",
        "-c",
        "core.symlinks=false",
        "-c",
        f"protocol.file.allow={'always' if local else 'never'}",
        "clone",
        "--quiet",
        "--no-recurse-submodules",
        "--",
        url,
        dest,
    ]
    try:
        proc = subprocess.run(
            cmd, env=git_env(), capture_output=True, timeout=CLONE_TIMEOUT, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, onerror=_force_remove)
        raise RepoError(f"Clone timed out after {CLONE_TIMEOUT} s: {url}", 3) from None
    except FileNotFoundError:
        shutil.rmtree(tmp, onerror=_force_remove)
        raise RepoError("git is not installed or not on PATH.", 3) from None
    if proc.returncode != 0:
        shutil.rmtree(tmp, onerror=_force_remove)
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RepoError(f"Clone failed for {url}: {err[-1] if err else 'unknown error'}", 3)
    repo = Repo(url=url if not local else source, root=Path(dest).resolve(), _tmp=tmp)
    try:
        build_index(repo)
        build_profile(repo)
    except Exception:
        repo.cleanup()
        raise
    return repo


def build_index(repo: Repo) -> None:
    out = run_git(["ls-files", "-z"], repo.root)
    for rel in filter(None, out.split("\0")):
        p = repo.root / rel
        try:
            st = p.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            repo.files[rel] = FileInfo(size=0, lines=None, binary=True)
            continue
        size = st.st_size
        binary, lines = False, None
        if size <= MAX_TEXT_BYTES:
            data = p.read_bytes()
            binary = b"\0" in data[:8192]
            if not binary:
                lines = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
        else:
            binary = True
        repo.files[rel] = FileInfo(size=size, lines=lines, binary=binary)


def build_profile(repo: Repo) -> None:
    try:
        commits = int(run_git(["rev-list", "--count", "HEAD"], repo.root).strip())
    except (RuntimeError, ValueError):
        commits = 0
    try:
        branch = run_git(["symbolic-ref", "--short", "HEAD"], repo.root).strip()
    except RuntimeError:
        branch = "unknown"

    exts = Counter()
    top_dirs = Counter()
    for path in repo.files:
        name = path.rsplit("/", 1)[-1]
        ext = os.path.splitext(name)[1].lower() or name
        exts[ext] += 1
        if "/" in path:
            top_dirs[path.split("/", 1)[0] + "/"] += 1

    present, absent = {}, []
    for key, patterns in KEY_FILES.items():
        hits = sorted({p for pat in patterns for p in repo.glob(pat)})
        if hits:
            present[key] = hits[:8]
        else:
            absent.append(key)

    big_dirs = {}
    dir_counts = Counter()
    for path in repo.files:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dir_counts["/".join(parts[:i]) + "/"] += 1
    for d, n in dir_counts.items():
        base = d.rstrip("/").rsplit("/", 1)[-1]
        if base in (
            "node_modules",
            "venv",
            ".venv",
            "dist",
            "build",
            "__pycache__",
            "site-packages",
            "vendor",
            "target",
            ".tox",
            "env",
        ):
            big_dirs[d] = n

    repo.profile = {
        "files": len(repo.files),
        "commits": commits,
        "branch": branch,
        "file_types": dict(exts.most_common(10)),
        "top_level_dirs": dict(top_dirs.most_common(15)),
        "key_files_present": present,
        "key_files_absent": absent,
        "vendored_or_generated_dirs": big_dirs,
    }


def profile_text(repo: Repo) -> str:
    p = repo.profile
    lines = [
        f"{p['files']} tracked files, {p['commits']} commits, branch {p['branch']}.",
        "File types: " + ", ".join(f"{k} {v}" for k, v in p["file_types"].items()),
        "Top-level dirs: " + (", ".join(f"{k} ({v})" for k, v in p["top_level_dirs"].items()) or "(none)"),
        "Key files present:",
    ]
    for key, hits in p["key_files_present"].items():
        lines.append(f"  {key}: {', '.join(hits)}")
    lines.append("Key file groups with no match: " + (", ".join(p["key_files_absent"]) or "(none)"))
    if p["vendored_or_generated_dirs"]:
        lines.append(
            "Tracked vendored/generated dirs: "
            + ", ".join(f"{k} ({v} files)" for k, v in p["vendored_or_generated_dirs"].items())
        )
    return "\n".join(lines)
