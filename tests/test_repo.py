import os

import pytest

from auditor.repo import RepoError, clone, normalize_url, profile_text
from tests.conftest import SAMPLE_COMMITS


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/psf/requests", "https://github.com/psf/requests"),
        ("https://github.com/psf/requests.git", "https://github.com/psf/requests"),
        ("https://github.com/psf/requests/", "https://github.com/psf/requests"),
        (" https://github.com/a-b/c.d_e ", "https://github.com/a-b/c.d_e"),
    ],
)
def test_normalize_valid(url, expected):
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/psf/requests",
        "https://gitlab.com/a/b",
        "https://github.com/psf",
        "https://github.com/psf/requests/tree/main",
        "git@github.com:psf/requests.git",
        "file:///etc/passwd",
        "https://github.com/-bad/x",
        "",
    ],
)
def test_normalize_invalid(url):
    with pytest.raises(RepoError) as e:
        normalize_url(url)
    assert e.value.exit_code == 2


def test_local_path_rejected_without_flag(tmp_path):
    with pytest.raises(RepoError) as e:
        clone(str(tmp_path))
    assert e.value.exit_code == 2


def test_clone_index_profile(git_repo_factory):
    repo = git_repo_factory(SAMPLE_COMMITS)
    assert repo.root.is_dir()
    assert repo.files["app/config.py"].lines == 5
    assert repo.files["app/main.pyc"].binary
    p = repo.profile
    assert p["commits"] == 3
    assert p["branch"] == "main"
    assert p["key_files_present"]["github_workflows"] == [".github/workflows/ci.yml"]
    assert ".env" in p["key_files_present"]["env_files"]
    assert "lockfiles" in p["key_files_absent"]
    text = profile_text(repo)
    assert "3 commits" in text and "github_workflows" in text


def test_cleanup_removes_clone(git_repo_factory):
    repo = git_repo_factory(SAMPLE_COMMITS)
    tmp = repo._tmp
    repo.cleanup()
    assert not os.path.exists(tmp)


def test_clone_does_not_change_source(tmp_path, git_repo_factory):
    git_repo_factory(SAMPLE_COMMITS)
    src = tmp_path / "src"
    before = sorted(p.name for p in src.iterdir())
    assert ".git" in before and "README.md" in before


def test_clone_failure_exit_code(tmp_path):
    empty = tmp_path / "notarepo"
    empty.mkdir()
    with pytest.raises(RepoError) as e:
        clone(str(empty), allow_local=True)
    assert e.value.exit_code == 3
