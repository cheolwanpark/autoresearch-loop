from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_loop.campaign import Campaign, validate_plan
from research_loop.storage import atomic_json
from research_loop.workspace import git
from conftest import plan, task, worker_done


def test_completion_driven_pool_and_distinct_models(harness):
    c, jobs, clock = harness
    coord = jobs.requests[c.job_path(c.state["coordinator"])]
    assert coord["model"] == "gpt-6.1-sol" and coord["cwd"] == str(c.path)
    plan(c, jobs, [task("slow"), task("fast"), task("third")])
    for exp in c.active():
        request = jobs.requests[c.job_path(exp)]
        assert request["model"] == "gpt-6-luna"
        assert Path(request["cwd"]) == c.path / "worktrees" / exp["id"]
    fast = worker_done(c, jobs, "e000002")
    jobs.complete(c, fast, evaluation={"score": 9, "passed": True})
    c.tick()
    assert c.state["best"] == "e000002"
    assert c.state["experiments"]["e000001"]["stage"] == "agent"
    assert c.state["coordinator"]["slots"] == 1
    plan(c, jobs, [task("follow-up", "e000002")])
    assert c.state["experiments"]["e000004"]["stage"] == "agent"
    clock.advance(86400 * 3)
    c.tick()
    assert not jobs.stopped  # No silence or elapsed-time heuristic.


def test_combination_revalidated_and_worse_candidate_retained(harness):
    c, jobs, clock = harness
    plan(c, jobs, [task("a"), task("b")])
    a = worker_done(c, jobs, "e000001")
    b = worker_done(c, jobs, "e000002", "a = 0\nb = 1\n")
    assert b["stage"] == "eval_ready"  # Evaluation concurrency = 1, slot retained.
    jobs.complete(c, a, evaluation={"score": 9, "passed": True})
    c.tick()
    jobs.complete(c, b, evaluation={"score": 9, "passed": True})
    # A coordinator may still be interpreting the first completion.
    plan(c, jobs, [])
    clock.advance()
    c.tick()
    plan(c, jobs, [task("combine", "e000001", ["e000002"])])
    combined = worker_done(c, jobs, "e000003", "a = 1\nb = 1\n")
    jobs.complete(c, combined, evaluation={"score": 13, "passed": True})
    c.tick()
    assert c.state["best"] == "e000001"  # Tie preserves incumbent; bad combo rejected.
    assert git(c.repo, "rev-parse", c.state["branch"]) == a["commit"]
    assert (
        git(c.repo, "rev-parse", f"refs/research/{c.state['run_id']}/e000003") == combined["commit"]
    )
    assert combined["parent"] == "e000001" and combined["combine"] == ["e000002"]


def test_forbidden_changes_repair_then_reject_without_evaluation(harness):
    c, jobs, _ = harness
    plan(c, jobs, [task()])
    exp = c.state["experiments"]["e000001"]
    cwd = c.path / "worktrees" / exp["id"]
    (cwd / "evaluate.py").write_text("raise SystemExit(0)\n")
    for i in range(3):
        jobs.complete(c, exp, text="done", thread_id="t")
        c.tick()
        if i < 2:
            c.tick()
    assert exp["stage"] == "rejected"
    assert not any(
        jobs.requests[p]["kind"] == "evaluation" for p in jobs.requests if "e000001" in str(p)
    )
    assert (c.repo / "evaluate.py").read_text().startswith("import json")


def test_forbidden_change_can_be_undone(harness):
    c, jobs, _ = harness
    plan(c, jobs, [task()])
    exp = c.state["experiments"]["e000001"]
    cwd = c.path / "worktrees" / exp["id"]
    (cwd / "evaluate.py").write_text("bad")
    jobs.complete(c, exp, text="done")
    c.tick()
    assert exp["stage"] == "worker_ready"
    (cwd / "evaluate.py").write_text((c.repo / "evaluate.py").read_text())
    c.tick()
    worker_done(c, jobs, "e000001")
    assert exp["stage"] == "eval"


def test_retry_json_repair_new_thread_and_auth_observable(harness):
    c, jobs, clock = harness
    owner = c.state["coordinator"]
    original_count = len(jobs.requests)
    for _ in range(3):
        path = c.job_path(owner)
        atomic_json(path / "agent.json", {"thread_id": "bad-thread"})
        jobs.complete(c, owner, text="not json", thread_id="bad-thread")
        c.tick()
        assert owner["stage"] == "idle" and "Invalid output" in owner["error"]
        clock.advance(301)
        c.tick()
    assert jobs.requests[c.job_path(owner)]["thread_id"] is None
    jobs.complete(c, owner, ok=False, error="401 unauthorized")
    c.tick()
    assert owner["retry_at"] == clock() + 1800
    clock.advance(1800)
    c.tick()
    assert len(jobs.requests) == original_count + 4
    plan(c, jobs, [task()])
    assert c.state["status"] == "running"


def test_duration_stops_dispatch_but_drains_and_interprets(harness):
    c, jobs, clock = harness
    plan(c, jobs, [task()])
    c.state["deadline"] = clock() + 1
    clock.advance(2)
    exp = worker_done(c, jobs, "e000001")
    assert c.state["status"] == "running"
    jobs.complete(c, exp, evaluation={"score": 8, "passed": True})
    c.tick()
    # Older plan was in flight when duration expired: its tasks must be discarded.
    jobs.complete(
        c, c.state["coordinator"], text=json.dumps({"summary": "old", "tasks": [task("late")]})
    )
    c.tick()
    assert c.state["next_id"] == 2
    jobs.complete(
        c, c.state["coordinator"], text=json.dumps({"summary": "final findings", "tasks": []})
    )
    assert not c.tick()
    assert c.state["status"] == "completed" and c.state["summary"] == "final findings"


def test_resume_recovers_commit_and_branch_window(harness):
    c, jobs, clock = harness
    plan(c, jobs, [task()])
    exp = worker_done(c, jobs, "e000001")
    old_commit = exp["commit"]
    exp["stage"] = "commit"
    c.save()
    resumed = Campaign(c.path, jobs=jobs, clock=clock, status=jobs.status)
    resumed.tick()
    exp = resumed.state["experiments"]["e000001"]
    assert exp["commit"] == old_commit
    jobs.complete(resumed, exp, evaluation={"score": 8, "passed": True})
    resumed.tick()
    # Simulate branch update missing after durable score completion.
    git(c.repo, "update-ref", c.state["branch"], c.contract["start_commit"])
    resumed = Campaign(c.path, jobs=jobs, clock=clock, status=jobs.status)
    resumed.reconcile()
    assert git(c.repo, "rev-parse", c.state["branch"]) == old_commit
    n = len(jobs.requests)
    resumed.tick()
    assert len(jobs.requests) == n


def test_unknown_liveness_never_duplicates(harness):
    c, jobs, clock = harness
    plan(c, jobs, [task()])
    exp = c.state["experiments"]["e000001"]
    path = c.job_path(exp)
    status = c.job_status
    c.job_status = lambda p: "uncertain" if p == path else status(p)
    n = len(jobs.requests)
    clock.advance(86400)
    c.tick()
    assert len(jobs.requests) == n and exp["observed"] == "uncertain"


def test_validate_references_counts_and_branching(harness):
    c, _, _ = harness
    assert validate_plan(json.dumps({"summary": "ok", "tasks": [task()]}), c.state, 1, "baseline")
    for tasks in (
        [task(base="missing")],
        [task(), task()],
        [task(base="baseline", combine=["missing"])],
    ):
        with pytest.raises(ValueError):
            validate_plan(json.dumps({"summary": "ok", "tasks": tasks}), c.state, 1, "baseline")


def test_repeated_no_tasks_does_not_finish(harness):
    c, jobs, clock = harness
    for _ in range(10):
        plan(c, jobs, [], "Research is complete")
        clock.advance(6)
        c.tick()
    assert c.state["status"] == "running"


def test_multi_day_restarts_errors_and_non_improvements(harness):
    c, jobs, clock = harness
    accepted = 10
    for index in range(18):
        # Transient errors and malformed JSON do not end a days-long campaign.
        if index % 3 == 0:
            jobs.complete(c, c.state["coordinator"], ok=False, error="rate limit", retry_after=23)
            c.tick()
            assert c.state["coordinator"]["retry_at"] == clock() + 23
            clock.advance(86400)
            c.tick()
        if index % 4 == 0:
            jobs.complete(c, c.state["coordinator"], text="malformed")
            c.tick()
            clock.advance(86400)
            c.tick()
        plan(c, jobs, [task(f"hypothesis-{index}", c.state["best"])])
        identifier = f"e{index + 1:06d}"
        exp = worker_done(c, jobs, identifier, f"a = {index}\nb = 0\n")
        passed = index % 5 != 0
        score = accepted - 1 if passed else accepted - 100
        jobs.complete(c, exp, evaluation={"score": score, "passed": passed})
        c.tick()
        if passed:
            accepted = score
        # Restart at saved boundaries, including an active coordinator job.
        c = Campaign(c.path, jobs=jobs, clock=clock, status=jobs.status)
        c.reconcile()
        n = len(jobs.requests)
        c.tick()
        assert len(jobs.requests) == n
        assert c.state["experiments"][c.state["best"]]["evaluation"]["score"] == accepted
        assert (
            git(c.repo, "rev-parse", c.state["branch"])
            == c.state["experiments"][c.state["best"]]["commit"]
        )
        assert c.state["status"] == "running"
    assert clock() >= 10000000000 + 10 * 86400
    assert len(list((c.path / "experiments").glob("*.json"))) == 19


def test_late_old_completion_is_in_context(harness):
    c, _, _ = harness
    # Recent records must be ordered by completion, not experiment allocation.
    template = c.state["experiments"]["baseline"]
    for i in range(40):
        c.state["experiments"][f"e{i}"] = template | {"id": f"e{i}", "sequence": i + 2}
    c.state["experiments"]["e0"]["sequence"] = 100
    c.state["coordinator"]["seen_revision"] = 99
    context = c.context(0)
    assert "e0" in {e["id"] for e in context["recent"]}


def test_explicit_worker_timeout_is_opt_in(harness):
    c, jobs, clock = harness
    c.config["worker_timeout"] = 1
    plan(c, jobs, [task()])
    exp = c.state["experiments"]["e000001"]
    clock.advance(2)
    c.tick()
    assert exp["stage"] == "rejected" and "explicit timeout" in exp["error"]
    assert jobs.stopped == [c.job_path(exp)]


def test_maximize_and_failed_high_score(harness):
    c, jobs, _ = harness
    c.config["direction"] = "maximize"
    plan(c, jobs, [task("valid"), task("invalid")])
    exp = worker_done(c, jobs, "e000001")
    jobs.complete(c, exp, evaluation={"score": 11, "passed": True})
    c.tick()
    assert c.state["best"] == exp["id"]
    other = worker_done(c, jobs, "e000002")
    jobs.complete(c, other, evaluation={"score": 100, "passed": False})
    c.tick()
    assert c.state["best"] == exp["id"]
