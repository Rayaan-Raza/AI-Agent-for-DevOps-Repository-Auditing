"""Local test repositories with known injected defects, for self-evaluation only.

The agent never sees this file or the expected lists. Each repository is a list of
commits; each commit maps a path to its content (None deletes the file).
"""

# Fake secrets: they look real to the agent but are not valid anywhere.
FAKE_AWS = "AKIA" + "Q3XK7PLM2RTV9WNB"
FAKE_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY99"
FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7x" + "Q" * 60 + "\n-----END RSA PRIVATE KEY-----\n"
)

# ---------------------------------------------------------------- repo A ---
# Obvious problems in a small Flask service.
REPO_A = [
    {
        "app/__init__.py": "",
        "app/main.py": (
            "from flask import Flask, jsonify\n\n"
            "from app import settings\n\n"
            "app = Flask(__name__)\n\n\n"
            "@app.route('/health')\n"
            "def health():\n"
            "    return jsonify(status='ok', data_dir=settings.DATA_DIR)\n\n\n"
            "if __name__ == '__main__':\n"
            "    app.run(host='0.0.0.0', debug=settings.DEBUG)\n"
        ),
        "app/settings.py": (
            "import os\n\n"
            "DEBUG = True\n"
            f'AWS_ACCESS_KEY_ID = "{FAKE_AWS}"\n'
            f'AWS_SECRET_ACCESS_KEY = "{FAKE_AWS_SECRET}"\n'
            'DATA_DIR = "C:/Users/jdoe/projects/inventory/data"\n'
            'LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")\n'
        ),
        "requirements.txt": "flask\nboto3\nrequests\ngunicorn\n",
    },
    {
        ".env": "DATABASE_URL=postgres://inventory:Pr0dPassw0rd2024@db.internal:5432/inventory\n"
        "SECRET_KEY=9f8e7d6c5b4a39281706f5e4d3c2b1a0\n",
        "app/__pycache__/main.cpython-311.pyc": b"\xa7\r\r\n\x00\x00\x00\x00binary",
        "app/__pycache__/settings.cpython-311.pyc": b"\xa7\r\r\n\x00\x00\x00\x00binary",
    },
    {"app/inventory.py": "def count(items):\n    return len(items)\n"},
]

EXPECTED_A = [
    {"category": "repository_hygiene", "file": None, "label": "missing README"},
    {
        "category": "repository_hygiene",
        "file": None,
        "alt_files": [".gitignore"],
        "label": "missing .gitignore",
    },
    {"category": "repository_hygiene", "file": "app/__pycache__/*", "label": "tracked .pyc files"},
    {"category": "security", "file": "app/settings.py", "label": "hardcoded AWS key"},
    {"category": "security", "file": ".env", "label": "committed .env"},
    {"category": "dependency_management", "file": "requirements.txt", "label": "unpinned deps"},
    {"category": "testing", "file": None, "label": "no tests"},
    {"category": "configuration", "file": "app/settings.py", "label": "machine-specific path"},
]

# ---------------------------------------------------------------- repo B ---
# A tidy-looking project with subtle CI, security and configuration defects.
_B_README = (
    "# shipit\n\nSmall CLI that packages release notes.\n\n## Setup\n\n"
    "```bash\npip install -e .[dev]\npytest\n```\n\n## Contributing\n\nOpen a pull request; CI must pass.\n"
)
_B_PYPROJECT = (
    '[project]\nname = "shipit"\nversion = "1.4.0"\nrequires-python = ">=3.10"\n'
    'dependencies = ["click>=8.1,<9", "jinja2>=3.1,<4"]\n\n'
    '[project.optional-dependencies]\ndev = ["pytest>=8,<9", "ruff>=0.5,<1"]\n\n'
    "[tool.ruff]\nline-length = 100\n"
)
_B_CI = (
    "name: CI\n"
    "on:\n"
    "  push:\n"
    "    branches: [main]\n"
    "  pull_request:\n"
    "permissions: write-all\n"
    "jobs:\n"
    "  test:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - uses: actions/setup-python@v5\n"
    "        with:\n"
    "          python-version: '3.12'\n"
    "      - run: pip install -e .[dev]\n"
    "      - run: ruff check .\n"
    "      - name: Tests\n"
    "        run: pytest -q || true\n"
)
_B_TRIAGE = (
    "name: PR triage\n"
    "on:\n"
    "  pull_request_target:\n"
    "    types: [opened, synchronize]\n"
    "jobs:\n"
    "  triage:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "        with:\n"
    "          ref: ${{ github.event.pull_request.head.sha }}\n"
    "      - name: Label\n"
    "        run: |\n"
    '          echo "Triage for ${{ github.event.pull_request.title }}"\n'
    "          python scripts/label.py\n"
    "        env:\n"
    "          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n"
)
_B_RELEASE = (
    "name: Release\n"
    "on:\n"
    "  push:\n"
    "    tags: ['v*']\n"
    "jobs:\n"
    "  publish:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
    "        with:\n"
    "          password: ${{ secrets.PYPI_TOKEN }}\n"
    '      - run: echo "Published with token ${{ secrets.PYPI_TOKEN }}"\n'
)

REPO_B = [
    {
        "README.md": _B_README,
        ".gitignore": "__pycache__/\n*.pyc\n.venv/\ndist/\n.env\n",
        "pyproject.toml": _B_PYPROJECT,
        "src/shipit/__init__.py": '__version__ = "1.4.0"\n',
        "src/shipit/cli.py": (
            "import click\n\n\n@click.command()\n@click.argument('version')\n"
            "def main(version):\n    click.echo(f'notes for {version}')\n"
        ),
        "tests/test_cli.py": (
            "from click.testing import CliRunner\n\nfrom shipit.cli import main\n\n\n"
            "def test_main():\n    assert CliRunner().invoke(main, ['1.0']).exit_code == 0\n"
        ),
        "scripts/label.py": "print('labelled')\n",
        ".github/workflows/ci.yml": _B_CI,
    },
    {"deploy/deploy_key.pem": FAKE_PEM},
    {"deploy/deploy_key.pem": None, ".github/workflows/triage.yml": _B_TRIAGE},
    {
        ".github/workflows/release.yml": _B_RELEASE,
        ".vscode/settings.json": '{\n  "python.defaultInterpreterPath": "/Users/mkhan/.venvs/shipit/bin/python"\n}\n',
    },
]

EXPECTED_B = [
    {"category": "ci", "file": ".github/workflows/ci.yml", "lines": [18], "label": "pytest || true"},
    {"category": "security", "file": ".github/workflows/ci.yml", "lines": [6], "label": "write-all"},
    {
        "category": "security",
        "file": ".github/workflows/triage.yml",
        "lines": [3, 9, 11, 15],
        "label": "pull_request_target runs PR head code",
    },
    {
        "category": "security",
        "file": ".github/workflows/triage.yml",
        "lines": [14],
        "label": "script injection from PR title",
    },
    {
        "category": "security",
        "file": ".github/workflows/release.yml",
        "lines": [13],
        "label": "secret echoed in logs",
    },
    {"category": "security", "file": "deploy/deploy_key.pem", "label": "private key in git history"},
    {"category": "configuration", "file": ".vscode/settings.json", "label": "committed local IDE settings"},
]

# ---------------------------------------------------------------- repo C ---
# A healthy library. The correct report is empty or nearly empty.
_C_CI = (
    "name: CI\n"
    "on:\n"
    "  push:\n"
    "    branches: [main]\n"
    "  pull_request:\n"
    "permissions:\n"
    "  contents: read\n"
    "jobs:\n"
    "  test:\n"
    "    runs-on: ubuntu-latest\n"
    "    strategy:\n"
    "      matrix:\n"
    "        python-version: ['3.11', '3.12']\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - uses: actions/setup-python@v5\n"
    "        with:\n"
    "          python-version: ${{ matrix.python-version }}\n"
    "      - run: pip install -e .[dev]\n"
    "      - run: ruff check .\n"
    "      - run: ruff format --check .\n"
    "      - run: pytest -q\n"
)

REPO_C = [
    {
        "README.md": (
            "# tidyconf\n\nLoad typed settings from environment variables.\n\n## Install\n\n"
            "```bash\npip install tidyconf\n```\n\n## Development\n\n"
            "```bash\npython -m venv .venv\npip install -e .[dev]\npytest\n```\n"
        ),
        "CONTRIBUTING.md": "# Contributing\n\nOpen a pull request. CI runs lint and tests. One approval is needed.\n",
        "LICENSE": "MIT License\n",
        ".gitignore": "__pycache__/\n*.py[cod]\n.venv/\ndist/\nbuild/\n.env\n.pytest_cache/\n.ruff_cache/\n",
        ".env.example": "TIDYCONF_PREFIX=\nTIDYCONF_DEBUG=false\n",
        "pyproject.toml": (
            '[project]\nname = "tidyconf"\nversion = "0.3.1"\nrequires-python = ">=3.11"\n'
            'dependencies = ["pydantic>=2.6,<3"]\n\n'
            '[project.optional-dependencies]\ndev = ["pytest>=8,<9", "ruff>=0.5,<1"]\n\n'
            '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E", "F", "I"]\n'
        ),
        "src/tidyconf/__init__.py": 'from tidyconf.loader import load\n\n__all__ = ["load"]\n',
        "src/tidyconf/loader.py": (
            'import os\n\n\ndef load(prefix: str = "APP_") -> dict[str, str]:\n'
            '    """Return environment variables that start with prefix."""\n'
            "    return {k[len(prefix):]: v for k, v in os.environ.items() if k.startswith(prefix)}\n"
        ),
        "tests/test_loader.py": (
            "from tidyconf import load\n\n\ndef test_load(monkeypatch):\n"
            '    monkeypatch.setenv("APP_X", "1")\n    assert load()["X"] == "1"\n'
        ),
        ".github/workflows/ci.yml": _C_CI,
        ".github/dependabot.yml": (
            "version: 2\nupdates:\n  - package-ecosystem: pip\n    directory: /\n    schedule:\n"
            "      interval: weekly\n  - package-ecosystem: github-actions\n    directory: /\n"
            "    schedule:\n      interval: weekly\n"
        ),
        ".pre-commit-config.yaml": (
            "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.5.0\n"
            "    hooks:\n      - id: ruff\n      - id: ruff-format\n"
        ),
    },
    {
        "src/tidyconf/loader.py": (
            'import os\n\n\ndef load(prefix: str = "APP_") -> dict[str, str]:\n'
            '    """Return environment variables that start with prefix, without the prefix."""\n'
            "    return {k[len(prefix):]: v for k, v in os.environ.items() if k.startswith(prefix)}\n"
        )
    },
]

EXPECTED_C: list[dict] = []

REPOS = {
    "repo_a_obvious": (REPO_A, EXPECTED_A),
    "repo_b_subtle": (REPO_B, EXPECTED_B),
    "repo_c_healthy": (REPO_C, EXPECTED_C),
}
