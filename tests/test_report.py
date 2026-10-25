import json

import jsonschema
import pytest

import tools
from auditor.report import REPORT_SCHEMA, build_report, validate_candidate, write_report
from auditor.state import AuditState
from tests.conftest import SAMPLE_COMMITS


@pytest.fixture
def ctx(git_repo_factory):
    repo = git_repo_factory(SAMPLE_COMMITS)
    return tools.ToolContext(state=AuditState(repo=repo))


def good(**over):
    base = {
        "rule_id": "SEC-001",
        "category": "security",
        "severity": "critical",
        "file": "app/config.py",
        "line": 4,
        "description": "Hardcoded API credential",
        "evidence": "API_KEY is assigned a literal value at line 4",
        "recommendation": "Read the key from an environment variable",
        "kind": "present",
        "confidence": "high",
    }
    base.update(over)
    return base


def test_line_must_be_seen(ctx):
    _, errors, _ = validate_candidate(ctx.state, good())
    assert any("have not looked" in e for e in errors)
    tools.read_file(ctx, "app/config.py")
    clean, errors, _ = validate_candidate(ctx.state, good())
    assert errors == [] and clean["line"] == 4


@pytest.mark.parametrize(
    "over,msg",
    [
        ({"severity": "urgent"}, "severity"),
        ({"category": "nonsense"}, "category"),
        ({"rule_id": "SEC1"}, "rule_id"),
        ({"rule_id": "CI-001"}, "prefix"),
        ({"file": "nope.py"}, "not a tracked file"),
        ({"line": 99}, "not in app/config.py"),
        ({"line": "x"}, "integer"),
        ({"file": None}, "line needs a file"),
        ({"evidence": ""}, "evidence is mandatory"),
        ({"description": "Enable branch protection on main"}, "branch protection"),
    ],
)
def test_rejections(ctx, over, msg):
    tools.read_file(ctx, "app/config.py")
    _, errors, _ = validate_candidate(ctx.state, good(**over))
    assert any(msg in e for e in errors), errors


def test_normalization_and_aliases(ctx):
    tools.read_file(ctx, "app/config.py")
    clean, errors, _ = validate_candidate(
        ctx.state,
        good(rule_id="sec-001", category="Security", severity="CRITICAL", file="./app\\config.py", line="4"),
    )
    assert not errors
    assert (clean["rule_id"], clean["category"], clean["severity"], clean["file"], clean["line"]) == (
        "SEC-001",
        "security",
        "critical",
        "app/config.py",
        4,
    )
    clean, errors, _ = validate_candidate(
        ctx.state, good(rule_id="CI-002", category="GitHub Actions", file=None, line="null")
    )
    assert not errors and clean["category"] == "ci" and clean["line"] is None


def test_absent_kind(ctx):
    _, errors, _ = validate_candidate(
        ctx.state,
        good(
            rule_id="HYG-001",
            category="repository_hygiene",
            file=None,
            line=None,
            kind="absent",
            absent_globs=["README*"],
        ),
    )
    assert any("not absent" in e for e in errors)
    clean, errors, _ = validate_candidate(
        ctx.state,
        good(
            rule_id="TST-001",
            category="testing",
            file=None,
            line=None,
            description="No automated tests",
            kind="absent",
            absent_globs=["tests/*", "**/test_*.py"],
        ),
    )
    assert not errors
    _, errors, _ = validate_candidate(
        ctx.state,
        good(rule_id="DEP-002", category="dependency_management", file=None, line=None, kind="absent"),
    )
    assert any("absent_globs" in e for e in errors)


def test_history_kind(git_repo_factory):
    repo = git_repo_factory([{"secret.txt": "x\n"}, {"secret.txt": None, "a.txt": "a\n"}])
    state = AuditState(repo=repo)
    clean, errors, _ = validate_candidate(
        state, good(rule_id="SEC-003", file="secret.txt", line=None, kind="history")
    )
    assert not errors
    _, errors, _ = validate_candidate(
        state, good(rule_id="SEC-003", file="never.txt", line=None, kind="history")
    )
    assert errors


def test_secret_in_text_redacted(ctx):
    tools.read_file(ctx, "app/config.py")
    clean, errors, warnings = validate_candidate(
        ctx.state, good(evidence='API_KEY = "sk_live_abcdefghijklmnop1234" in config')
    )
    assert not errors and "sk_live_" not in clean["evidence"] and warnings


def test_duplicate_detection_and_update(ctx):
    tools.read_file(ctx, "app/config.py")
    assert tools.record_violation(ctx, **good()).startswith("Recorded V1")
    assert "Duplicate of V1" in tools.record_violation(ctx, **good(severity="high"))
    assert tools.update_violation(ctx, "v1", severity="high").startswith("Updated V1")
    assert ctx.state.candidates[0].data["severity"] == "high"
    assert "REJECTED" in tools.update_violation(ctx, "V1", line=500)
    assert tools.retract_violation(ctx, "V1", "test").startswith("Retracted V1")
    with pytest.raises(tools.ToolError):
        tools.retract_violation(ctx, "V1", "again")


def test_build_report_filters_sorts_and_validates(ctx):
    tools.read_file(ctx, "app/config.py")
    tools.read_file(ctx, ".github/workflows/ci.yml")
    st = ctx.state
    st.add_candidate(
        validate_candidate(st, good(severity="low", rule_id="SEC-008", line=3, description="Debug on"))[0], 1
    )
    st.add_candidate(validate_candidate(st, good())[0], 2)
    st.add_candidate(
        validate_candidate(
            st,
            good(rule_id="CI-002", category="ci", severity="high", file=".github/workflows/ci.yml", line=13),
        )[0],
        3,
    )
    low = validate_candidate(st, good(rule_id="SEC-008", line=5, confidence="low"))[0]
    st.add_candidate(low, 4)
    retracted = st.add_candidate(validate_candidate(st, good(rule_id="SEC-002", line=1))[0], 5)
    retracted.retracted = True
    report = build_report(st, "https://github.com/x/y")
    sev = [v["severity"] for v in report["violations"]]
    assert sev == ["critical", "high", "low"]
    assert set(report["violations"][0]) == {
        "rule_id",
        "category",
        "severity",
        "file",
        "line",
        "description",
        "evidence",
        "recommendation",
    }
    jsonschema.validate(report, REPORT_SCHEMA)


def test_build_report_dedupes_keeps_higher_severity(ctx):
    tools.read_file(ctx, "app/config.py")
    st = ctx.state
    st.add_candidate(validate_candidate(st, good(severity="medium"))[0], 1)
    st.add_candidate(validate_candidate(st, good(severity="critical"), check_duplicates=False)[0], 2)
    report = build_report(st, "u")
    assert len(report["violations"]) == 1 and report["violations"][0]["severity"] == "critical"


def test_empty_report_and_atomic_write(ctx, tmp_path):
    report = build_report(ctx.state, "https://github.com/x/y")
    assert report == {"repository": "https://github.com/x/y", "violations": []}
    out = tmp_path / "sub" / "report.json"
    write_report(report, str(out))
    assert json.loads(out.read_text()) == report
    assert [p.name for p in out.parent.iterdir()] == ["report.json"]


def test_write_report_rejects_invalid(tmp_path):
    with pytest.raises(jsonschema.ValidationError):
        write_report({"repository": "x", "violations": [{"rule_id": "X"}]}, str(tmp_path / "r.json"))
    assert not (tmp_path / "r.json").exists()


# --- dispatcher ---------------------------------------------------------------


def test_execute_validates_and_coerces(ctx):
    text, args, ok = tools.execute(
        ctx, "read_file", '{"path": "README.md", "start_line": "2", "reason": "r", "junk": 1}'
    )
    assert ok and args == {"path": "README.md", "start_line": 2, "reason": "r"}
    assert "lines 2-3 of 3" in text


@pytest.mark.parametrize(
    "name,raw,msg",
    [
        ("nope", "{}", "Unknown tool"),
        ("read_file", "{bad json", "not valid JSON"),
        ("read_file", '{"reason": "r"}', "'path' is a required property"),
        ("read_file", '{"path": "x/../../y", "reason": "r"}', "outside the repository"),
        ("read_file", "[1]", "JSON object"),
        ("run_tests", '{"reason": "r"}', "not available"),
    ],
)
def test_execute_errors(ctx, name, raw, msg):
    text, _, ok = tools.execute(ctx, name, raw)
    assert not ok and text.startswith("ERROR") and msg in text


def test_schemas_are_valid_json_schema():
    for spec in tools.tool_schemas(tools.tool_names(True)):
        jsonschema.Draft202012Validator.check_schema(spec["function"]["parameters"])
    assert "run_tests" not in tools.tool_names(False)


def test_state_tools(ctx):
    out, _, ok = tools.execute(
        ctx, "update_coverage", {"areas": ["repository_hygiene", "ci"], "status": "examined", "reason": "x"}
    )
    assert ok and "repository_hygiene, ci -> examined" in out
    assert ctx.state.coverage["ci"] == "examined"
    # A bare string is accepted for the list.
    out, _, ok = tools.execute(
        ctx, "update_coverage", {"areas": "testing", "status": "not_applicable", "reason": "x"}
    )
    assert ok and ctx.state.coverage["testing"] == "not_applicable"
    _, _, ok = tools.execute(ctx, "update_coverage", {"areas": ["x"], "status": "examined", "reason": "x"})
    assert not ok
    assert "Note recorded" in tools.execute(ctx, "add_note", {"text": "CI runs on push only"})[0]
    tools.execute(ctx, "finish", {"summary": "done"})
    assert ctx.state.finished


def test_record_violations_list_and_coercion(ctx):
    tools.read_file(ctx, "app/config.py")
    items = [
        good(),
        good(rule_id="SEC-008", severity="low", line="3", description="Debug on"),
        good(file="missing.py"),
    ]
    out, _, ok = tools.execute(ctx, "record_violations", {"violations": items})
    assert ok
    assert "item 1: Recorded V1" in out and "item 2: Recorded V2" in out and "item 3: REJECTED" in out
    assert ctx.state.candidates[1].data["line"] == 3
    # A single object and a JSON string are accepted too.
    out, _, ok = tools.execute(
        ctx, "record_violations", {"violations": good(rule_id="SEC-009", line=5, description="x")}
    )
    assert ok and "Recorded V3" in out
    out, _, ok = tools.execute(
        ctx, "record_violations", {"violations": json.dumps([good(rule_id="SEC-010", line=1)])}
    )
    assert ok and "Recorded V4" in out


def test_reason_not_required_by_api_schema():
    for spec in tools.tool_schemas(tools.tool_names(True)):
        assert "reason" not in spec["function"]["parameters"]["required"]


def _dep(**over):
    base = dict(
        rule_id="DEP-001",
        category="dependency_management",
        severity="medium",
        file=None,
        line=None,
        description="Dependencies are unpinned",
        evidence="e",
        recommendation="r",
        kind="present",
        confidence="high",
    )
    base.update(over)
    return base


def test_dependency_guard_ranges_are_not_unpinned(git_repo_factory):
    repo = git_repo_factory([{"pyproject.toml": '[project]\nname = "x"\ndependencies = ["click>=8.1,<9"]\n'}])
    state = AuditState(repo=repo)
    _, errors, _ = validate_candidate(state, _dep(file="pyproject.toml"))
    assert any("properly constrained" in e for e in errors)
    _, errors, _ = validate_candidate(state, _dep(rule_id="DEP-002", description="No lockfile"))
    assert any("No tool that creates a lockfile" in e for e in errors)


def test_dependency_guard_allows_real_problems(git_repo_factory):
    repo = git_repo_factory(
        [{"requirements.txt": "flask\nrequests==2.0\n", "Pipfile": "[packages]\nflask = '*'\n"}]
    )
    state = AuditState(repo=repo)
    clean, errors, _ = validate_candidate(state, _dep(file="requirements.txt"))
    assert not errors
    clean, errors, _ = validate_candidate(
        state, _dep(rule_id="DEP-002", description="Pipfile.lock is missing")
    )
    assert not errors
    # DEP-003 mentions ranges but is about inconsistency: not blocked.
    clean, errors, _ = validate_candidate(
        state,
        _dep(rule_id="DEP-003", description="flask has a version range in one file and none in another"),
    )
    assert not errors


def test_dependency_guard_lockfile_present(git_repo_factory):
    repo = git_repo_factory([{"pyproject.toml": "[tool.poetry]\nname = 'x'\n", "poetry.lock": "#\n"}])
    _, errors, _ = validate_candidate(
        AuditState(repo=repo), _dep(rule_id="DEP-002", description="Missing lockfile")
    )
    assert any("A lockfile is present: poetry.lock" in e for e in errors)


_CD_WORKFLOW = (
    "on:\n  push:\n    tags: ['v*']\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - uses: actions/checkout@v4\n      - run: pip install -r requirements.txt\n"
    "      - name: Run tests\n        run: pytest\n"
)


def _ci(**over):
    base = dict(
        rule_id="CI-003",
        category="ci",
        severity="high",
        file=".github/workflows/cd.yml",
        line=None,
        description="CI workflow does not execute tests",
        evidence="The test job has no test step.",
        recommendation="Add a pytest step",
        kind="present",
        confidence="high",
    )
    base.update(over)
    return base


def test_ci_guard_rejects_false_no_tests_claim(git_repo_factory):
    state = AuditState(repo=git_repo_factory([{".github/workflows/cd.yml": _CD_WORKFLOW}]))
    _, errors, _ = validate_candidate(state, _ci())
    assert any("line 11 runs tests ('pytest')" in e for e in errors)


def test_ci_guard_allows_trigger_claims_and_real_gaps(git_repo_factory):
    state = AuditState(
        repo=git_repo_factory(
            [
                {
                    ".github/workflows/cd.yml": _CD_WORKFLOW,
                    ".github/workflows/lint.yml": (
                        "on: [push]\njobs:\n  l:\n    steps:\n      - run: ruff check .\n"
                    ),
                }
            ]
        )
    )
    # "Tests run only on tags, not on pull requests" is a real finding.
    clean, errors, _ = validate_candidate(
        state,
        _ci(
            rule_id="TST-003",
            category="testing",
            description="Tests do not run on pull requests or pushes, only on version tags",
        ),
    )
    assert not errors
    # A workflow that really has no test command.
    clean, errors, _ = validate_candidate(state, _ci(file=".github/workflows/lint.yml"))
    assert not errors
