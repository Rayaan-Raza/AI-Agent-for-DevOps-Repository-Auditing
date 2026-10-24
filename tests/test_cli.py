import json
import subprocess
import sys
from pathlib import Path

import pytest

import agent
from auditor.llm import LLMAuthError, LLMError
from auditor.state import AREAS
from tests.conftest import SAMPLE_COMMITS, make_git_repo
from tests.fakes import FakeLLM, call, reply

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.setattr(agent, "load_env", lambda: None)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def test_invalid_url_exit_2(tmp_path):
    assert agent.main(["--repo", "not-a-url", "--output", str(tmp_path / "r.json")]) == 2
    assert not (tmp_path / "r.json").exists()


def test_missing_key_exit_4(tmp_path):
    assert agent.main(["--repo", "https://github.com/a/b", "--output", str(tmp_path / "r.json")]) == 4


def test_output_is_directory_exit_2(tmp_path):
    assert agent.main(["--repo", "https://github.com/a/b", "--output", str(tmp_path)]) == 2


def test_missing_args_exit_2():
    with pytest.raises(SystemExit) as e:
        agent.main([])
    assert e.value.code == 2


def test_clone_failure_exit_3(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = agent.main(
        ["--repo", str(empty), "--output", str(tmp_path / "r.json"), "--allow-local"],
        llm_factory=lambda: FakeLLM([]),
    )
    assert code == 3


@pytest.mark.parametrize("exc,code", [(LLMAuthError("bad key"), 4), (LLMError("down"), 5)])
def test_llm_errors(tmp_path, exc, code):
    src = make_git_repo(tmp_path / "src", SAMPLE_COMMITS)
    out = tmp_path / "r.json"
    assert (
        agent.main(
            ["--repo", str(src), "--output", str(out), "--allow-local"], llm_factory=lambda: FakeLLM([exc])
        )
        == code
    )
    assert not out.exists()


def test_end_to_end_with_fake_llm(tmp_path, capsys):
    src = make_git_repo(tmp_path / "src", SAMPLE_COMMITS)
    before = subprocess.run(["git", "status", "--porcelain"], cwd=src, capture_output=True, text=True).stdout
    script = [
        reply(call("git_tracked_ignored", reason="look for committed local files")),
        reply(call("read_file", path=".env", reason=".env is tracked")),
        reply(
            call(
                "record_violation",
                rule_id="SEC-002",
                category="security",
                severity="high",
                file=".env",
                line=1,
                description="Committed .env with a password",
                evidence="DB_PASSWORD has a literal value",
                recommendation="Remove .env from git",
                kind="present",
                confidence="high",
            )
        ),
        *[reply(call("update_coverage", areas=[a], status="examined", reason="x")) for a in AREAS],
        reply(call("finish", summary="done")),
        reply(call("finish", summary="reviewed")),
    ]
    out = tmp_path / "out" / "report.json"
    code = agent.main(
        ["--repo", str(src), "--output", str(out), "--allow-local"], llm_factory=lambda: FakeLLM(script)
    )
    assert code == 0
    report = json.loads(out.read_text())
    assert report["violations"][0]["rule_id"] == "SEC-002"
    assert "hunter2" not in out.read_text()
    trace = (tmp_path / "out" / "report.trace.jsonl").read_text()
    assert "hunter2" not in trace and "git_tracked_ignored" in trace
    after = subprocess.run(["git", "status", "--porcelain"], cwd=src, capture_output=True, text=True).stdout
    assert before == after


def test_empty_report_when_nothing_found(tmp_path):
    src = make_git_repo(tmp_path / "src", [{"README.md": "# x\n"}])
    script = [
        *[reply(call("update_coverage", areas=[a], status="not_applicable", reason="tiny")) for a in AREAS],
        reply(call("finish", summary="nothing")),
    ]
    out = tmp_path / "r.json"
    assert (
        agent.main(
            ["--repo", str(src), "--output", str(out), "--allow-local", "-q"],
            llm_factory=lambda: FakeLLM(script),
        )
        == 0
    )
    assert json.loads(out.read_text())["violations"] == []


def test_help_runs_as_script():
    proc = subprocess.run([sys.executable, "agent.py", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0 and "--repo" in proc.stdout and "--allow-local" not in proc.stdout


def test_secret_never_in_stderr(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "Z" * 40)

    def boom():
        raise RuntimeError("failed with key gsk_" + "Z" * 40)

    src = make_git_repo(tmp_path / "src", SAMPLE_COMMITS)
    code = agent.main(
        ["--repo", str(src), "--output", str(tmp_path / "r.json"), "--allow-local"], llm_factory=boom
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Z" * 40 not in err and "REDACTED" in err
