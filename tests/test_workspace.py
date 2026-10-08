import pytest

from research_loop.campaign import create
from research_loop.job_runner import parse_score
from research_loop.storage import retry_delay, read_json
from research_loop.workspace import commit_candidate, git, violations, worktree


def test_dirty_checkout_excluded_and_contract_frozen(repository):
    original = git(repository, "show", "HEAD:candidate.py")
    (repository / "candidate.py").write_text("uncommitted = True\n")
    path = create(repository / "research.toml")
    contract = read_json(path / "contract.json")
    cwd = path / "worker"
    worktree(repository, cwd, contract["start_commit"])
    assert (cwd / "candidate.py").read_text().strip() == original
    (repository / "program.md").write_text("changed after launch")
    assert contract["instructions"]["program"] != (repository / "program.md").read_text()
    assert (repository / "candidate.py").read_text() == "uncommitted = True\n"
    assert git(repository, "check-ignore", str(path))


def test_symlinks_and_staged_renames_rejected(repository, tmp_path):
    base = git(repository, "rev-parse", "HEAD")
    cwd = tmp_path / "work"
    worktree(repository, cwd, base)
    (cwd / "candidate.py").unlink()
    (cwd / "candidate.py").symlink_to(repository / "candidate.py")
    assert violations(cwd, base, ["candidate.py"]) == ["candidate.py"]
    (cwd / "candidate.py").unlink()
    git(cwd, "restore", "candidate.py")
    git(cwd, "mv", "evaluate.py", "candidate2.py")
    assert "evaluate.py" in violations(cwd, base, ["candidate2.py"])


def test_commit_recovery_and_stale_index(repository, tmp_path):
    base = git(repository, "rev-parse", "HEAD")
    cwd = tmp_path / "work"
    worktree(repository, cwd, base)
    (cwd / "evaluate.py").write_text("bad")
    git(cwd, "add", "evaluate.py")
    (cwd / "evaluate.py").write_text((repository / "evaluate.py").read_text())
    (cwd / "candidate.py").write_text("a=1\nb=0\n")
    commit = commit_candidate(cwd, base, "owned commit", ["candidate.py"])
    assert git(cwd, "show", f"{commit}:evaluate.py") == git(
        repository, "show", f"{base}:evaluate.py"
    )
    assert commit_candidate(cwd, base, "owned commit", ["candidate.py"]) == commit


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{}",
        '{"score": NaN, "passed": true}',
        '{"score": true, "passed": true}',
        '{"score": 1, "passed": 1}',
        '{"score": 1, "passed": true, "extra": Infinity}',
    ],
)
def test_score_protocol_invalid(text):
    with pytest.raises((ValueError, TypeError)):
        parse_score(text)


def test_score_final_line_and_retry_policies():
    assert parse_score('diagnostics\n{"score": 0.42, "passed": true}\n')["score"] == 0.42
    assert retry_delay("network", 1, jitter=lambda: 0) == 5
    assert retry_delay("network", 100, jitter=lambda: 1) == 300
    assert retry_delay("rate limit", 1, 123) == 123
    assert retry_delay("try again in 72s", 1) == 72
    assert retry_delay("model not available", 1) == 1800
    assert retry_delay("No space left on device", 1) == 1800
