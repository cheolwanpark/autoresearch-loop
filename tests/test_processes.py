from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from research_loop.campaign import Campaign, create
from research_loop.jobs import Jobs, job_status
from research_loop.storage import atomic_json, lock, read_json
from research_loop.workspace import git


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.025)
    raise AssertionError("test timed out waiting for a process event")


def test_evaluation_stdin_path_and_duplicate_lock(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    path = tmp_path / "job"
    jobs = Jobs()
    request = {
        "kind": "evaluation",
        "cwd": str(cwd),
        "command": [
            "sh",
            "-c",
            'read value; test -z "$value"; echo run >> count; sleep 0.2; echo \'{"score": 1, "passed": true}\'',
        ],
    }
    try:
        jobs.launch(path, request)
        jobs.launch(path, request)
        wait_for(lambda: job_status(path) == "done")
        assert (cwd / "count").read_text() == "run\n"
        assert read_json(path / "result.json")["evaluation"] == {"score": 1, "passed": True}
        jobs.launch(path, request)
        assert (cwd / "count").read_text() == "run\n"
    finally:
        jobs.stop([path])


def test_scheduler_crash_recovers_live_evaluation_without_duplicate(repository):
    marker = repository / "started"
    evaluator = (
        'import time,json\nfrom pathlib import Path\np=Path("count")\np.write_text(p.read_text()+"x" if p.exists() else "x")\nPath('
        + repr(str(marker))
        + ').write_text("started")\ntime.sleep(1.5)\nprint(json.dumps({"score": 1, "passed": True}))\n'
    )
    (repository / "evaluate.py").write_text(evaluator)
    git(repository, "add", "evaluate.py")
    git(repository, "commit", "-qm", "slow evaluation")
    path = create(repository / "research.toml")
    # Start only the baseline; no model request is made in this process test.
    script = 'from pathlib import Path; import time; from research_loop.campaign import Campaign; c=Campaign(Path(__import__("sys").argv[1])); c.start_evaluations(); time.sleep(100)'
    scheduler = subprocess.Popen(
        [sys.executable, "-c", script, str(path)], stdin=subprocess.DEVNULL
    )
    try:
        wait_for(marker.exists)
        state = read_json(path / "state.json")
        job = path / state["experiments"]["baseline"]["job"]
        assert job_status(job) == "running"
        scheduler.kill()
        scheduler.wait()
        recovered = Campaign(path)
        original_job = recovered.state["experiments"]["baseline"]["job"]
        recovered.progress_experiment(recovered.state["experiments"]["baseline"])
        assert recovered.state["experiments"]["baseline"]["job"] == original_job
        wait_for(lambda: job_status(job) == "done")
        recovered.progress_experiment(recovered.state["experiments"]["baseline"])
        assert recovered.state["best"] == "baseline"
        cwd = Path(read_json(job / "request.json")["cwd"])
        assert (cwd / "count").read_text() == "x"
    finally:
        if scheduler.poll() is None:
            scheduler.kill()
            scheduler.wait()
        Jobs().stop(list((path / "jobs").glob("*/*")))


def test_explicit_stop_kills_child_group(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    job = tmp_path / "job"
    jobs = Jobs()
    try:
        jobs.launch(
            job,
            {
                "kind": "evaluation",
                "cwd": str(cwd),
                "command": [sys.executable, "-c", "import time; time.sleep(100)"],
            },
        )
        wait_for(lambda: (job / "owner.json").exists())
        jobs.stop([job])
        wait_for(lambda: job_status(job) == "dead")
    finally:
        jobs.stop([job])


def test_process_identity_boot_and_pid_reuse(tmp_path, monkeypatch):
    from research_loop import jobs

    owner = {"pid": 123, "pgid": 123, "started": "old", "command": "original", "boot": "boot"}
    atomic_json(tmp_path / "owner.json", owner)
    monkeypatch.setattr(jobs, "boot_id", lambda: "boot")
    monkeypatch.setattr(
        jobs,
        "processes",
        lambda: {123: {"pgid": 999, "started": "new", "command": "other", "status": "S"}},
    )
    assert job_status(tmp_path) == "dead"
    monkeypatch.setattr(
        jobs,
        "processes",
        lambda: {124: {"pgid": 123, "started": "old", "command": "orphan", "status": "S"}},
    )
    assert job_status(tmp_path) == "uncertain"
    monkeypatch.setattr(jobs, "boot_id", lambda: "rebooted")
    assert job_status(tmp_path) == "dead"


def test_campaign_lock_blocks_second_writer(tmp_path):
    with lock(tmp_path / "lock"):
        with pytest.raises(BlockingIOError):
            with lock(tmp_path / "lock"):
                pass


def test_supervisor_restarts_crashed_scheduler_and_collects_result(repository, tmp_path):
    (repository / "evaluate.py").write_text(
        'import json,time\nfrom pathlib import Path\np=Path("count")\np.write_text(p.read_text()+"x" if p.exists() else "x")\ntime.sleep(1)\nprint(json.dumps({"score": 1, "passed": True}))\n'
    )
    git(repository, "add", "evaluate.py")
    git(repository, "commit", "-qm", "slow baseline")
    path = create(repository / "research.toml")
    inner = tmp_path / "inner.py"
    inner.write_text("""import os,signal,sys,time
from pathlib import Path
from research_loop.campaign import Campaign
from research_loop.jobs import job_status
from research_loop.storage import atomic_json
p=Path(sys.argv[1]); c=Campaign(p)
b=c.state["experiments"]["baseline"]
if not (p/"crashed").exists():
    c.start_evaluations()
    while not (c.job_path(b)/"owner.json").exists(): time.sleep(.01)
    (p/"crashed").touch()
    os.kill(os.getpid(), signal.SIGKILL)
while job_status(c.job_path(b)) != "done": time.sleep(.01)
c.progress_experiment(b)
c.state["status"]="completed"
c.save()
""")
    code = "from pathlib import Path; import sys; from research_loop.cli import supervise; supervise(Path(sys.argv[1]), inner_command=[sys.executable, sys.argv[2]])"
    supervisor = subprocess.Popen(
        [sys.executable, "-c", code, str(path), str(inner)], stdin=subprocess.DEVNULL
    )
    try:
        assert supervisor.wait(timeout=20) == 0
        state = read_json(path / "state.json")
        assert state["status"] == "completed"
        assert read_json(path / "supervisor.json")["restarts"] == 1
        baseline = state["experiments"]["baseline"]
        assert len(baseline["jobs"]) == 1
        cwd = Path(read_json(path / baseline["job"] / "request.json")["cwd"])
        assert (cwd / "count").read_text() == "x"
        assert git(repository, "rev-parse", state["branch"]) == baseline["commit"]
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=5)
        Jobs().stop(list((path / "jobs").glob("*/*")))


def test_stop_immediately_after_launch(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    path = tmp_path / "job"
    jobs = Jobs()
    jobs.launch(
        path,
        {
            "kind": "evaluation",
            "cwd": str(cwd),
            "command": [sys.executable, "-c", "import time;time.sleep(100)"],
        },
    )
    handles = list(jobs.children)
    jobs.stop([path])
    assert all(p.poll() is not None for p in handles)


def test_supervisor_explicit_stop_no_restart(repository):
    (repository / "evaluate.py").write_text("import time\ntime.sleep(100)\n")
    git(repository, "add", "evaluate.py")
    git(repository, "commit", "-qm", "long evaluation")
    path = create(repository / "research.toml")
    supervisor = subprocess.Popen(
        [sys.executable, "-m", "research_loop.cli", "resume", str(path)], stdin=subprocess.DEVNULL
    )
    try:
        job = wait_for(
            lambda: next(iter((path / "jobs").glob("baseline/*/owner.json")), None)
        ).parent
        supervisor.terminate()
        assert supervisor.wait(timeout=10) == 0
        assert read_json(path / "state.json")["status"] == "stopped"
        assert read_json(path / "supervisor.json")["restarts"] == 0
        assert job_status(job) == "dead"
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait()
        Jobs().stop(list((path / "jobs").glob("*/*")))


def test_new_supervisor_observes_surviving_scheduler(repository):
    (repository / "evaluate.py").write_text("import time\ntime.sleep(100)\n")
    git(repository, "add", "evaluate.py")
    git(repository, "commit", "-qm", "long evaluation")
    path = create(repository / "research.toml")
    command = [sys.executable, "-m", "research_loop.cli", "resume", str(path)]
    old = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    new = None
    try:
        job = wait_for(
            lambda: next(iter((path / "jobs").glob("baseline/*/owner.json")), None)
        ).parent
        scheduler = read_json(path / "scheduler.json")
        old.kill()
        old.wait()
        new = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert new.stdout.readline().startswith("campaign:")
        assert "existing scheduler is alive" in new.stdout.readline()
        assert read_json(path / "scheduler.json") == scheduler
        assert len(read_json(path / "state.json")["experiments"]["baseline"]["jobs"]) == 1
        new.terminate()
        assert new.wait(timeout=10) == 0
        assert read_json(path / "state.json")["status"] == "stopped"
        assert job_status(job) == "dead"
    finally:
        for process in (old, new):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        Jobs().stop(list((path / "jobs").glob("*/*")))


def test_official_evaluation_uses_clean_frozen_candidate(harness):
    from conftest import plan, task, worker_done

    c, fake, _ = harness
    plan(c, fake, [task()])
    # Editing the original checkout cannot change the campaign's evaluator.
    (c.repo / "evaluate.py").write_text('print(\'{"score": -999, "passed": true}\')\n')
    cwd = c.path / "worktrees" / "e000001"
    (cwd / ".research-local").mkdir(exist_ok=True)
    (cwd / ".research-local" / "uncommitted").write_text("worker scratch")
    exp = worker_done(c, fake, "e000001", "a=1\nb=0\n")
    job = c.job_path(exp)
    real = Jobs()
    try:
        real.launch(job, read_json(job / "request.json"))
        wait_for(lambda: job_status(job) == "done")
        assert read_json(job / "result.json")["evaluation"]["score"] == 9
        evaluation_cwd = Path(read_json(job / "request.json")["cwd"])
        assert not (evaluation_cwd / ".research-local" / "uncommitted").exists()
        c.tick()
        assert c.state["best"] == exp["id"]
    finally:
        real.stop([job])
