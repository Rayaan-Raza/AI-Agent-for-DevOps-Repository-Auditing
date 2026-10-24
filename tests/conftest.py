import os
import subprocess
from pathlib import Path

import pytest

from auditor.repo import clone


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00", GIT_COMMITTER_DATE="2026-01-01T00:00:00")
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def make_git_repo(path: Path, commits: list[dict[str, str | None]]) -> Path:
    """Make a git repository. Each commit maps file path -> content (None deletes the file)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "core.autocrlf", "false")
    for i, files in enumerate(commits):
        for rel, content in files.items():
            f = path / rel
            if content is None:
                _git(path, "rm", "-q", rel)
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                f.write_bytes(content)
            else:
                f.write_text(content, encoding="utf-8", newline="\n")
            _git(path, "add", "-f", rel)
        _git(path, "commit", "-q", "--allow-empty", "-m", f"commit {i + 1}")
    return path


@pytest.fixture
def git_repo_factory(tmp_path):
    clones = []

    def factory(commits, name="src"):
        src = make_git_repo(tmp_path / name, commits)
        repo = clone(str(src), allow_local=True)
        clones.append(repo)
        return repo

    yield factory
    for r in clones:
        r.cleanup()


SAMPLE_COMMITS = [
    {
        "README.md": "# Demo\n\nInstall with pip.\n",
        "app/config.py": 'import os\n\nDEBUG = True\nAPI_KEY = "sk_live_abcdefghijklmnop1234"\n'
        'DB_URL = os.environ["DB_URL"]\n',
        "requirements.txt": "flask\nrequests>=2.0\npytest==8.0.0\n",
        ".gitignore": "*.pyc\n.env\n",
        ".github/workflows/ci.yml": (
            "name: CI\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "permissions: write-all\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-python@main\n"
            "      - run: pip install -r requirements.txt\n"
            "      - run: pytest || true\n"
            "        continue-on-error: true\n"
            '      - run: echo "${{ github.event.head_commit.message }}"\n'
            "        env:\n"
            "          TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n"
        ),
    },
    {".env": "DB_PASSWORD=hunter2hunter2\nDEBUG=1\n", "app/main.pyc": b"\x00\x01binary"},
    {"app/main.py": "from app.config import DEBUG\n\n\ndef main():\n    return DEBUG\n"},
]
