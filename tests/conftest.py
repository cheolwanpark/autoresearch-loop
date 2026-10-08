from __future__ import annotations

import json

import pytest

from research_loop.campaign import Campaign, create
from research_loop.storage import atomic_json
from research_loop.workspace import git


class Clock:
    def __init__(self):
        self.now = 10000000000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=10):
        self.now += seconds


class FakeJobs:
    def __init__(self):
        self.requests = {}
        self.stopped = []

    def launch(self, path, request):
        self.requests.setdefault(path, request)

    def reap(self):
        pass

    def stop(self, paths):
        self.stopped.extend(paths)
        for path in paths:
            self.result(path, ok=False, error="stopped")

    def status(self, path):
        return (
            "done"
            if (path / "result.json").exists()
            else "running"
            if path in self.requests
            else "new"
        )

    def result(self, path, **result):
        atomic_json(path / "result.json", {"ok": True, **result})

    def complete(self, campaign, owner, **result):
        self.result(campaign.job_path(owner), **result)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "candidate.py").write_text("a = 0\nb = 0\n")
    (repo / "evaluate.py").write_text(
        'import json\nfrom candidate import a,b\nprint(json.dumps({"score": 10-a-b+5*a*b, "passed": True}))\n'
    )
    (repo / "program.md").write_text("Improve score without changing the evaluator.")
    (repo / "coordinator.md").write_text("Plan distinct hypotheses and combinations.")
    (repo / "worker.md").write_text("Edit candidate.py only. No Git mutations.")
    (repo / "research.toml").write_text("""repository = "."
program = "program.md"
coordinator_instructions = "coordinator.md"
worker_instructions = "worker.md"
allowed_paths = ["candidate.py"]
protected_paths = ["evaluate.py"]
evaluation_command = ["python3", "evaluate.py"]
""")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    return repo


@pytest.fixture
def harness(repository):
    path = create(repository / "research.toml")
    clock, jobs = Clock(), FakeJobs()
    campaign = Campaign(path, jobs=jobs, clock=clock, status=jobs.status)
    campaign.tick()
    jobs.complete(
        campaign,
        campaign.state["experiments"]["baseline"],
        evaluation={"score": 10, "passed": True},
    )
    campaign.tick()
    return campaign, jobs, clock


def plan(campaign, jobs, tasks, summary="Accumulated findings"):
    jobs.complete(
        campaign,
        campaign.state["coordinator"],
        text=json.dumps({"summary": summary, "tasks": tasks}),
        thread_id="coordinator-thread",
    )
    campaign.tick()
    campaign.tick()


def task(name="first", base="baseline", combine=None):
    return {
        "hypothesis": name,
        "base": base,
        "instructions": "Edit one parameter",
        "combine": combine or [],
    }


def worker_done(campaign, jobs, identifier, source="a = 1\nb = 0\n"):
    exp = campaign.state["experiments"][identifier]
    cwd = campaign.path / "worktrees" / identifier
    (cwd / "candidate.py").write_text(source)
    jobs.complete(campaign, exp, text="Implemented and checked", thread_id=f"thread-{identifier}")
    campaign.tick()
    return exp
