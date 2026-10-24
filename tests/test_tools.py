import re

import pytest

import tools
from auditor.state import AuditState
from tests.conftest import SAMPLE_COMMITS


@pytest.fixture
def ctx(git_repo_factory):
    repo = git_repo_factory(SAMPLE_COMMITS)
    return tools.ToolContext(state=AuditState(repo=repo))


def make_ctx(git_repo_factory, files, name="r"):
    repo = git_repo_factory([files], name=name)
    return tools.ToolContext(state=AuditState(repo=repo))


# --- safe_path ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "C:/Windows", "a/../../b", ".git/config"])
def test_safe_path_rejects(ctx, bad):
    with pytest.raises(tools.ToolError):
        tools.safe_path(ctx, bad)


def test_safe_path_normalizes(ctx):
    assert tools.safe_path(ctx, "./app//config.py")[0] == "app/config.py"
    assert tools.safe_path(ctx, ".")[0] == "."


# --- list_files --------------------------------------------------------------


def test_list_files_shows_hidden_and_collapses(ctx):
    out = tools.list_files(ctx, ".", depth=1)
    assert ".env" in out and ".gitignore" in out
    assert "app/  (3 files below)" in out
    assert ".github/  (1 files below)" in out


def test_list_files_depth_and_pattern(ctx):
    out = tools.list_files(ctx, ".", depth=3, pattern="*.yml")
    assert ".github/workflows/ci.yml" in out
    assert "README.md" not in out


def test_list_files_collapses_vendored(git_repo_factory):
    files = {f"node_modules/pkg{i}/index.js": "x\n" for i in range(5)}
    files["index.js"] = "y\n"
    c = make_ctx(git_repo_factory, files)
    out = tools.list_files(c, ".", depth=5)
    assert "node_modules/  (5 files below)" in out


def test_list_files_missing_dir(ctx):
    with pytest.raises(tools.ToolError):
        tools.list_files(ctx, "nope")


# --- read_file ---------------------------------------------------------------


def test_read_file_numbers_redacts_and_marks_seen(ctx):
    out = tools.read_file(ctx, "app/config.py")
    assert out.startswith("app/config.py  lines 1-5 of 5")
    assert "sk_live_" not in out
    assert '4  API_KEY = "<REDACTED:' in out
    assert ctx.state.was_seen("app/config.py", 4)


def test_read_file_env_redacted(ctx):
    out = tools.read_file(ctx, ".env")
    assert "hunter2" not in out
    assert "DEBUG=1" in out


def test_read_file_range_and_paging(git_repo_factory):
    c = make_ctx(git_repo_factory, {"big.txt": "".join(f"line {i}\n" for i in range(1, 451))})
    out = tools.read_file(c, "big.txt", start_line=10, end_line=12)
    assert "big.txt  lines 10-12 of 450" in out and "line 11" in out
    out = tools.read_file(c, "big.txt")
    assert "lines 1-200 of 450" in out
    assert "start_line=201" in out
    assert not c.state.was_seen("big.txt", 201)


def test_read_file_errors(ctx):
    with pytest.raises(tools.ToolError, match="binary"):
        tools.read_file(ctx, "app/main.pyc")
    with pytest.raises(tools.ToolError, match="after the end"):
        tools.read_file(ctx, "README.md", start_line=999)
    with pytest.raises(tools.ToolError, match="directory"):
        tools.read_file(ctx, "app")


# --- search_code -------------------------------------------------------------


def test_search_code_finds_and_redacts(ctx):
    out = tools.search_code(ctx, r"API_KEY\s*=")
    assert "app/config.py:4:" in out
    assert "sk_live_" not in out
    assert ctx.state.was_seen("app/config.py", 4)


def test_search_code_glob_and_case(ctx):
    out = tools.search_code(ctx, "debug", path_glob="**/*.py", ignore_case=True)
    assert "app/config.py:3" in out and "app/main.py:1" in out
    assert ".env" not in out


def test_search_code_no_match_and_bad_regex(ctx):
    assert "No CONTENT matches" in tools.search_code(ctx, "zzz_not_here")
    with pytest.raises(tools.ToolError, match="Invalid regular expression"):
        tools.search_code(ctx, "(unclosed")


def test_search_code_catastrophic_pattern_does_not_hang(git_repo_factory):
    c = make_ctx(git_repo_factory, {"a.txt": "a" * 5000 + "!\n"})
    out = tools.search_code(c, r"(a+)+$")
    assert "timed out" in out or "No CONTENT matches" in out


def test_search_code_max_results(git_repo_factory):
    c = make_ctx(git_repo_factory, {"a.txt": "hit\n" * 30})
    out = tools.search_code(c, "hit", max_results=5)
    assert "30 content matches in 1 files (first 5 shown)" in out


# --- git_log -----------------------------------------------------------------


def test_git_log_basic(ctx):
    out = tools.git_log(ctx)
    assert len(re.findall(r"(?m)^commit [0-9a-f]", out)) == 3
    assert "Test User" in out and "test@example.com" not in out


def test_git_log_search_patch_redacts_history(git_repo_factory):
    commits = [
        {"settings.py": 'SECRET_KEY = "abcdef1234567890"\n'},
        {"settings.py": 'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n'},
    ]
    repo = git_repo_factory(commits)
    c = tools.ToolContext(state=AuditState(repo=repo))
    out = tools.git_log(c, search="abcdef1234567890", show_patch=True)
    # The pickaxe search finds both the add and the removal, but the value is hidden.
    assert len(re.findall(r"(?m)^commit [0-9a-f]", out)) == 2
    assert "abcdef1234567890" not in out
    assert "<REDACTED:generic_secret" in out


def test_git_log_path_filter(ctx):
    out = tools.git_log(ctx, path=".env")
    assert len(re.findall(r"(?m)^commit [0-9a-f]", out)) == 1
    assert "No commits match" in tools.git_log(ctx, path="nothing.txt")


# --- inspect_workflows -------------------------------------------------------


def test_inspect_workflows_facts(ctx):
    out = tools.inspect_workflows(ctx)
    assert "trigger: push" in out and "[line 3]" in out
    assert "top-level permissions: write-all  [line 5]" in out
    assert "uses actions/checkout@v4  ref: tag" in out
    assert "uses actions/setup-python@main  ref: branch or other" in out
    assert "run: pytest || true  [line 13]" in out
    assert "continue-on-error: true  [line 14]" in out
    assert "DEPLOY_TOKEN (line 17)" in out
    assert "github.event.head_commit.message" in out
    assert ctx.state.was_seen(".github/workflows/ci.yml", 13)


def test_inspect_workflows_pull_request_target_and_checkout_ref(git_repo_factory):
    wf = (
        "on:\n"
        "  pull_request_target:\n"
        "    types: [opened]\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    permissions:\n"
        "      contents: read\n"
        "    steps:\n"
        "      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n"
    )
    c = make_ctx(git_repo_factory, {".github/workflows/pr.yaml": wf})
    out = tools.inspect_workflows(c)
    assert "trigger: pull_request_target" in out
    assert "ref: sha" in out
    assert "with ref: ${{ github.event.pull_request.head.sha }}" in out
    assert "permissions: {contents: read}" in out


def test_inspect_workflows_invalid_yaml_and_none(git_repo_factory):
    c = make_ctx(git_repo_factory, {".github/workflows/bad.yml": "on: [push\njobs: {\n"})
    assert "YAML parse error" in tools.inspect_workflows(c)
    c2 = make_ctx(git_repo_factory, {"a.txt": "x\n"}, name="r2")
    assert "No GitHub Actions workflow" in tools.inspect_workflows(c2)


# --- inspect_dependencies ----------------------------------------------------


@pytest.mark.parametrize(
    "spec,kind",
    [
        ("", "none"),
        ("==1.2.3", "exact"),
        (">=1.0", "lower_only"),
        (">=1,<2", "range"),
        ("~=1.4", "range"),
        ("==1.*", "range"),
        ("<3", "upper_only"),
        (">=1.0; python_version<'3.11'", "lower_only"),
        ("@ https://x/y.whl", "url_or_path"),
    ],
)
def test_classify_python(spec, kind):
    assert tools.classify_python(spec) == kind


@pytest.mark.parametrize(
    "spec,kind",
    [
        ("^1.2", "range"),
        ("~1.2", "range"),
        ("*", "none"),
        ("1.2.3", "exact"),
        (">=1", "lower_only"),
        ({"git": "https://x"}, "url_or_path"),
        ({"version": "^2"}, "range"),
    ],
)
def test_classify_poetry(spec, kind):
    assert tools.classify_poetry(spec) == kind


@pytest.mark.parametrize(
    "spec,kind",
    [
        ("^1.0.0", "range"),
        ("1.0.0", "exact"),
        ("*", "none"),
        ("latest", "none"),
        (">=2.0.0", "lower_only"),
        ("github:a/b", "url_or_path"),
    ],
)
def test_classify_npm(spec, kind):
    assert tools.classify_npm(spec) == kind


def test_inspect_dependencies_mixed(git_repo_factory):
    files = {
        "requirements.txt": "-r base.txt\nflask\nrequests==2.31.0  # pinned\n",
        "base.txt": "click>=8\n",
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["flask>=2,<3", "rich"]\n',
        "package.json": '{"dependencies": {"left-pad": "^1.3.0"}, "devDependencies": {"jest": "*"}}\n',
        "poetry.lock": "# lock\n",
    }
    c = make_ctx(git_repo_factory, files)
    out = tools.inspect_dependencies(c)
    assert "line 2: flask (no version)  -> none" in out
    assert "requests ==2.31.0  -> exact" in out
    assert "click >=8  -> lower_only" in out
    assert "flask >=2,<3  -> range" in out
    assert "left-pad ^1.3.0  -> range" in out and "jest *  -> none" in out
    assert "Lockfiles present: poetry.lock" in out
    assert "flask: " in out  # different constraints in two files
    assert "has no requires-python" in out
    assert c.state.was_seen("requirements.txt", 2)


def test_inspect_dependencies_none_and_parse_error(git_repo_factory):
    c = make_ctx(git_repo_factory, {"a.py": "x = 1\n"})
    assert "No dependency manifests" in tools.inspect_dependencies(c)
    c2 = make_ctx(git_repo_factory, {"pyproject.toml": "[project\n", "requirements.txt": "a\n"}, name="r2")
    assert "parse error" in tools.inspect_dependencies(c2)


# --- git_tracked_ignored -----------------------------------------------------


def test_git_tracked_ignored(ctx):
    out = tools.git_tracked_ignored(ctx)
    ignored_part = out.split("common local")[0]
    assert ".env" in ignored_part and "app/main.pyc" in ignored_part
    assert ".env  (pattern .env)" in out


def test_git_tracked_ignored_clean(git_repo_factory):
    c = make_ctx(git_repo_factory, {".gitignore": "*.log\n", "a.py": "x\n", ".env.example": "A=\n"})
    out = tools.git_tracked_ignored(c)
    assert out.count("(none)") == 2


# --- run_ruff ----------------------------------------------------------------


def test_run_ruff(git_repo_factory):
    c = make_ctx(git_repo_factory, {"a.py": "import os\n\n\ndef f():\n    return 1\n"})
    out = tools.run_ruff(c)
    assert "ruff checked 1 Python files" in out
    assert "F401" in out
    assert "a.py:1: F401" in out
    assert c.state.was_seen("a.py", 1)


def test_run_ruff_no_python(git_repo_factory):
    c = make_ctx(git_repo_factory, {"a.js": "x\n"})
    assert "No Python files" in tools.run_ruff(c)


def test_run_tests_disabled(ctx):
    assert "not available" in tools.run_tests(ctx)


def test_tools_do_not_modify_clone(ctx):
    import subprocess

    tools.list_files(ctx)
    tools.read_file(ctx, "README.md")
    tools.search_code(ctx, "x")
    tools.git_log(ctx, show_patch=True)
    tools.inspect_workflows(ctx)
    tools.inspect_dependencies(ctx)
    tools.git_tracked_ignored(ctx)
    tools.run_ruff(ctx)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--ignored"], cwd=ctx.root, capture_output=True, text=True
    ).stdout
    assert status.strip() == ""


def test_search_code_reports_path_matches(ctx):
    out = tools.search_code(ctx, r"README|\.gitignore")
    assert "PATH matches: .gitignore, README.md" in out
    out = tools.search_code(ctx, "Dockerfile")
    assert "No tracked file PATH matches." in out and "No CONTENT matches" in out


def test_search_code_glob_only_lists_files(ctx):
    out = tools.search_code(ctx, "", path_glob="**/*.py")
    assert out == "2 tracked files match '**/*.py': app/config.py, app/main.py"
    assert "0 tracked files match" in tools.search_code(ctx, "", path_glob="*.toml")
    with pytest.raises(tools.ToolError):
        tools.search_code(ctx, "")


def test_inspect_workflows_accepts_directory(ctx):
    assert "trigger: push" in tools.inspect_workflows(ctx, ".")
    assert "trigger: push" in tools.inspect_workflows(ctx, ".github/workflows")
