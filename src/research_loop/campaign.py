"""A single-writer, completion-driven pool. No rounds, database, or agent framework."""

from __future__ import annotations

import asyncio
import json
import signal
import time
import uuid
from pathlib import Path

from .config import load_config
from .jobs import Jobs, identity, job_status
from .storage import atomic_json, lock, read_json, retry_delay
from .workspace import commit_candidate, git, ignore_runs, set_ref, violations, worktree

TERMINAL = {"done", "rejected"}


def create(config_path: Path, seconds: float | None = None) -> Path:
    config = load_config(config_path)
    repo = Path(config["repository"])
    if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError("repository must be the root of an existing Git checkout")
    start = git(repo, "rev-parse", "HEAD^{commit}")
    if git(repo, "ls-files", ".research"):
        raise ValueError(".research must not contain tracked files")
    ignore_runs(repo)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    path = repo / ".research" / run_id
    path.mkdir(parents=True)
    instructions = {}
    protected = list(config["protected_paths"])
    for key in ("program", "coordinator_instructions", "worker_instructions"):
        source = Path(config[key])
        instructions[key] = source.read_text()
        if source.is_relative_to(repo):
            protected.append(str(source.relative_to(repo)))
    # Freeze direct script/executable arguments too. For indirect imports/configs,
    # users list evaluator dependencies in protected_paths.
    for index, arg in enumerate(config["evaluation_command"]):
        source = repo / arg
        if source.is_file():
            if source.resolve().is_relative_to(repo):
                relative = str(source.resolve().relative_to(repo))
                protected.append(relative)
                # Resolve repository scripts against each clean evaluation worktree.
                config["evaluation_command"][index] = "./" + relative
                git(repo, "cat-file", "-e", f"{start}:{relative}")
            elif index > 0:
                raise ValueError("evaluator files must be committed inside the target repository")
    config["protected_paths"] = sorted(set(protected))
    atomic_json(
        path / "contract.json",
        {"config": config, "instructions": instructions, "start_commit": start},
    )
    now = time.time()
    state = {
        "version": 1,
        "run_id": run_id,
        "branch": f"refs/heads/research/{run_id}",
        "created": now,
        "deadline": now + seconds if seconds is not None else None,
        "status": "running",
        "best": None,
        "summary": "",
        "revision": 0,
        "coordinator": {"stage": "idle", "seen_revision": -1},
        "next_id": 1,
        "experiments": {
            "baseline": {
                "id": "baseline",
                "parent": None,
                "base_commit": start,
                "commit": start,
                "hypothesis": "Fixed starting baseline",
                "instructions": "",
                "combine": [],
                "stage": "eval_ready",
                "jobs": [],
                "attempts": 0,
            }
        },
    }
    atomic_json(path / "state.json", state)
    set_ref(repo, state["branch"], start)
    return path


def validate_plan(text: str, state: dict, slots: int, snapshot_best: str | None) -> dict:
    plan = json.loads(text)
    if (
        not isinstance(plan, dict)
        or not isinstance(plan.get("summary"), str)
        or len(plan["summary"]) > 8000
    ):
        raise ValueError("return an object with summary (<=8000 characters) and tasks")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or len(tasks) > slots:
        raise ValueError(f"tasks must be an array with at most {slots} entries")
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {
            "hypothesis",
            "base",
            "instructions",
            "combine",
        }:
            raise ValueError("task keys: hypothesis, base, instructions, optional combine")
        for key in ("hypothesis", "base", "instructions"):
            if not isinstance(task.get(key), str) or not task[key].strip():
                raise ValueError(f"task requires nonempty {key}")
        combination = task.get("combine", [])
        if not isinstance(combination, list) or not all(isinstance(i, str) for i in combination):
            raise ValueError("combine must contain experiment IDs")
        for identifier in [task["base"], *combination]:
            exp = state["experiments"].get(identifier)
            if not exp or exp["stage"] != "done" or not exp.get("evaluation", {}).get("passed"):
                raise ValueError(f"{identifier!r} is not an evaluated, passing candidate")
        if combination and task["base"] != snapshot_best:
            raise ValueError("combination experiments must start from the current best")
    return plan


class Campaign:
    def __init__(self, path: Path, *, jobs=None, clock=time.time, status=job_status):
        self.path = path.resolve()
        self.contract = read_json(self.path / "contract.json")
        self.config = self.contract["config"]
        self.repo = Path(self.config["repository"])
        self.state = read_json(self.path / "state.json")
        self.jobs = jobs or Jobs()
        self.clock = clock
        self.job_status = status
        self.stop_requested = False
        self.last_status = 0.0
        self._saved = json.dumps(self.state, sort_keys=True)
        self._reconciled_revision = -1

    def save(self):
        encoded = json.dumps(self.state, sort_keys=True)
        if encoded != self._saved:
            atomic_json(self.path / "state.json", self.state)
            self._saved = encoded

    def log(self, message: str, **data):
        with (self.path / "events.jsonl").open("a") as log:
            log.write(
                json.dumps({"time": self.clock(), "message": message, **data}, ensure_ascii=False)
                + "\n"
            )
            log.flush()

    def expired(self):
        return self.state["deadline"] is not None and self.clock() >= self.state["deadline"]

    def active(self):
        return [e for e in self.state["experiments"].values() if e["stage"] not in TERMINAL]

    def job_path(self, owner: dict) -> Path:
        return self.path / owner["job"]

    def submit(self, owner: dict, kind: str, request: dict):
        index = len(owner.setdefault("jobs", [])) + 1
        name = owner.get("id", "coordinator")
        relative = f"jobs/{name}/{index:06d}-{kind}"
        owner["job"] = relative
        owner["jobs"].append(relative)
        owner["job_started"] = self.clock()
        owner["stage"] = "eval" if kind == "evaluation" else "agent"
        request["kind"] = kind
        path = self.path / relative
        atomic_json(path / "request.json", request)
        self.save()  # Persist launch intent before any side effect.
        self.jobs.launch(path, request)

    def outcome(self, owner: dict) -> dict | None:
        path = self.job_path(owner)
        status = self.job_status(path)
        timeout = (
            self.config["evaluation_timeout" if owner["stage"] == "eval" else "worker_timeout"]
            if owner.get("id")
            else None
        )
        if status == "new":
            self.jobs.launch(path, read_json(path / "request.json"))
        elif status == "done":
            return read_json(path / "result.json")
        elif status == "dead":
            metadata = read_json(path / "agent.json") if (path / "agent.json").exists() else {}
            owner.update(
                {
                    k: v
                    for k, v in metadata.items()
                    if k in ("thread_id", "turn_id", "prior_turn_ids")
                }
            )
            log = path / "wrapper.log"
            detail = ""
            if log.exists():
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 2000))
                    detail = stream.read().decode(errors="replace")
            return {
                "ok": False,
                "error": "job process exited without a result: " + detail,
                "recover": True,
            }
        elif timeout is not None and self.clock() - owner["job_started"] >= timeout:
            self.jobs.stop([path])
            if self.job_status(path) in ("dead", "done"):
                return {"ok": False, "error": f"explicit timeout after {timeout}s", "timeout": True}
        owner["observed"] = status
        return None

    def retry(self, owner: dict, result: dict, stage: str):
        owner["attempts"] = owner.get("attempts", 0) + 1
        delay = (
            1800
            if result.get("persistent")
            else retry_delay(result["error"], owner["attempts"], result.get("retry_after"))
        )
        owner.update(stage=stage, retry_at=self.clock() + delay, error=result["error"])
        owner["recover"] = bool(result.get("recover"))
        owner["recover_turn"] = owner.get("turn_id") if owner["recover"] else None
        # Preserve the thread created before a transport error as well.
        if owner.get("job"):
            meta = self.job_path(owner) / "agent.json"
            if meta.exists():
                owner["thread_id"] = read_json(meta).get("thread_id")
        self.log(
            "retry",
            owner=owner.get("id", "coordinator"),
            error=owner["error"],
            retry_at=owner["retry_at"],
        )

    def ready(self, owner: dict):
        return self.clock() >= owner.get("retry_at", 0)

    def instructions(self, role: str) -> str:
        return (
            self.contract["instructions"]["program"]
            + "\n\n"
            + self.contract["instructions"][role + "_instructions"]
        )

    def worker_request(self, exp: dict) -> dict:
        cwd = self.path / "worktrees" / exp["id"]
        worktree(self.repo, cwd, exp["base_commit"])
        combinations = [
            {
                "id": i,
                "commit": self.state["experiments"][i]["commit"],
                "base_commit": self.state["experiments"][i]["base_commit"],
            }
            for i in exp["combine"]
        ]
        prompt = json.dumps(
            {
                "hypothesis": exp["hypothesis"],
                "instructions": exp["instructions"],
                "allowed_paths": self.config["allowed_paths"],
                "protected_paths": self.config["protected_paths"],
                "base_commit": exp["base_commit"],
                "combine": combinations,
                "feedback": exp.get("feedback", ""),
                "recovery": bool(exp.get("recover_turn")),
            },
            ensure_ascii=False,
        )
        return {
            "role": "worker",
            "model": self.config["worker_model"],
            "cwd": str(cwd),
            "instructions": self.instructions("worker"),
            "prompt": prompt,
            "thread_id": exp.get("thread_id"),
            "recover_turn": exp.get("recover_turn"),
            "recover": exp.get("recover", False),
            "prior_turn_ids": exp.get("prior_turn_ids", []),
        }

    def finish(self, exp: dict, evaluation=None, error=None):
        exp.update(stage="done" if evaluation is not None else "rejected", finished=self.clock())
        if evaluation is not None:
            exp["evaluation"] = evaluation
        if error:
            exp["error"] = error
        self.state["revision"] += 1
        exp["sequence"] = self.state["revision"]
        atomic_json(self.path / "experiments" / f"{exp['id']}.json", exp)
        self.log("experiment completed", id=exp["id"], evaluation=evaluation, error=error)
        self.save()
        self.reconcile()

    def reconcile(self):
        best = None
        for exp in sorted(self.state["experiments"].values(), key=lambda e: e.get("sequence", 0)):
            if exp["stage"] != "done" or not exp.get("evaluation", {}).get("passed"):
                continue
            if best is None or (
                exp["evaluation"]["score"] < best["evaluation"]["score"]
                if self.config["direction"] == "minimize"
                else exp["evaluation"]["score"] > best["evaluation"]["score"]
            ):
                best = exp
        self.state["best"] = best["id"] if best else None
        set_ref(
            self.repo,
            self.state["branch"],
            best["commit"] if best else self.contract["start_commit"],
        )
        self._reconciled_revision = self.state["revision"]

    def progress_experiment(self, exp: dict):
        if exp["stage"] in ("agent", "eval"):
            stage = exp["stage"]
            result = self.outcome(exp)
            if result is None:
                return
            if not result["ok"]:
                if result.get("timeout") and exp["id"] != "baseline":
                    self.finish(exp, error=result["error"])
                else:
                    self.retry(exp, result, "worker_ready" if stage == "agent" else "eval_ready")
                return
            exp["attempts"] = 0
            exp.pop("retry_at", None)
            if stage == "eval":
                if exp["id"] == "baseline" and not result["evaluation"]["passed"]:
                    self.retry(
                        exp, {"error": result.get("error", "baseline did not pass")}, "eval_ready"
                    )
                else:
                    self.finish(exp, result["evaluation"], result.get("error"))
                return
            exp["thread_id"] = result.get("thread_id")
            exp["worker_response"] = result.get("text", "")
            exp.update(recover=False, recover_turn=None)
            exp["stage"] = "commit"
            self.save()
        if exp["stage"] == "commit":
            cwd = self.path / "worktrees" / exp["id"]
            invalid = violations(
                cwd,
                exp["base_commit"],
                self.config["allowed_paths"],
                self.config["protected_paths"],
            )
            if invalid:
                exp["repairs"] = exp.get("repairs", 0) + 1
                exp["feedback"] = (
                    f"Undo all forbidden changes before continuing: {invalid}. Do not run Git mutations."
                )
                if exp["repairs"] >= 3:
                    self.finish(exp, error=exp["feedback"])
                else:
                    exp["stage"] = "worker_ready"
                return
            try:
                exp["commit"] = commit_candidate(
                    cwd,
                    exp["base_commit"],
                    f"research {self.state['run_id']} {exp['id']}",
                    self.config["allowed_paths"],
                    self.config["protected_paths"],
                )
            except ValueError as exc:
                self.finish(exp, error=str(exc))
                return
            set_ref(self.repo, f"refs/research/{self.state['run_id']}/{exp['id']}", exp["commit"])
            exp["stage"] = "eval_ready"
            self.save()
        if exp["stage"] == "worker_ready" and self.ready(exp):
            self.submit(exp, "agent", self.worker_request(exp))

    def start_evaluations(self):
        count = sum(e["stage"] == "eval" for e in self.active())
        for exp in self.active():
            if count >= self.config["evaluation_concurrency"]:
                break
            if exp["stage"] == "eval_ready" and self.ready(exp):
                cwd = self.path / "evaluations" / f"{exp['id']}-{len(exp['jobs']) + 1}"
                worktree(self.repo, cwd, exp["commit"])
                self.submit(
                    exp,
                    "evaluation",
                    {"cwd": str(cwd), "command": self.config["evaluation_command"]},
                )
                count += 1

    def context(self, slots: int) -> dict:
        records = list(self.state["experiments"].values())
        completed = sorted(
            (e for e in records if e["stage"] in TERMINAL), key=lambda e: e["sequence"]
        )
        seen = self.state["coordinator"]["seen_revision"]
        # Include every unseen completion, even a very old, slow experiment.
        recent_ids = {e["id"] for e in completed[-30:]}
        selected = [e for e in completed if e["id"] in recent_ids or e["sequence"] > seen]
        selected += self.active()
        keys = (
            "id",
            "parent",
            "commit",
            "hypothesis",
            "combine",
            "stage",
            "evaluation",
            "error",
            "job",
            "worker_response",
        )
        return {
            "slots": slots,
            "best": self.state["best"],
            "summary": self.state["summary"],
            "recent": [
                {k: (e[k][:4000] if isinstance(e[k], str) else e[k]) for k in keys if k in e}
                for e in selected
            ],
            "active": [{"id": e["id"], "hypothesis": e["hypothesis"]} for e in self.active()],
            "records": str(self.path / "experiments"),
            "feedback": self.state["coordinator"].get("feedback", ""),
        }

    def progress_coordinator(self):
        owner = self.state["coordinator"]
        if owner["stage"] == "agent":
            result = self.outcome(owner)
            if result is None:
                return
            if not result["ok"]:
                self.retry(owner, result, "idle")
                return
            owner["thread_id"] = result.get("thread_id")
            try:
                plan = validate_plan(
                    result.get("text", ""), self.state, owner["slots"], owner["snapshot_best"]
                )
            except (ValueError, TypeError) as exc:
                owner["output_errors"] = owner.get("output_errors", 0) + 1
                owner["feedback"] = f"Invalid output: {exc}. Return only the required JSON."
                if owner["output_errors"] >= 3:
                    owner.update(thread_id=None, output_errors=0)
                    owner["feedback"] += " Replan in a fresh thread from saved records."
                self.retry(owner, {"error": owner["feedback"]}, "idle")
                # retry() reads persisted metadata; enforce the fresh-thread boundary.
                if owner["output_errors"] == 0:
                    owner["thread_id"] = None
                return
            owner.update(
                stage="idle",
                attempts=0,
                output_errors=0,
                feedback="",
                thread_id=None,
                recover_turn=None,
                recover=False,
            )
            owner["seen_revision"] = owner["snapshot_revision"]
            self.state["summary"] = plan["summary"]
            # Expiration during a coordinator call prevents dispatch of its proposals.
            if not self.expired():
                for task in plan["tasks"]:
                    identifier = f"e{self.state['next_id']:06d}"
                    self.state["next_id"] += 1
                    parent = self.state["best"] if task.get("combine") else task["base"]
                    exp = {
                        "id": identifier,
                        "parent": parent,
                        "base_commit": self.state["experiments"][parent]["commit"],
                        "hypothesis": task["hypothesis"],
                        "instructions": task["instructions"],
                        "combine": task.get("combine", []),
                        "stage": "worker_ready",
                        "jobs": [],
                        "attempts": 0,
                    }
                    self.state["experiments"][identifier] = exp
                    atomic_json(self.path / "proposals" / f"{identifier}.json", exp)
            if not plan["tasks"]:
                owner["retry_at"] = self.clock() + 5  # Completion claims never end the campaign.
            else:
                owner.pop("retry_at", None)
            self.save()
        if owner["stage"] == "idle" and self.state["best"] and self.ready(owner):
            slots = 0 if self.expired() else self.config["workers"] - len(self.active())
            if slots <= 0 and owner["seen_revision"] >= self.state["revision"]:
                return
            owner.update(
                slots=slots,
                snapshot_best=self.state["best"],
                snapshot_revision=self.state["revision"],
            )
            self.submit(
                owner,
                "agent",
                {
                    "role": "coordinator",
                    "cwd": str(self.path),
                    "model": self.config["coordinator_model"],
                    "instructions": self.instructions("coordinator"),
                    "prompt": json.dumps(self.context(slots), ensure_ascii=False),
                    "thread_id": owner.get("thread_id"),
                    "recover_turn": owner.get("recover_turn"),
                    "recover": owner.get("recover", False),
                    "prior_turn_ids": owner.get("prior_turn_ids", []),
                },
            )

    def tick(self) -> bool:
        self.jobs.reap()
        if self._reconciled_revision != self.state["revision"]:
            self.reconcile()
        for exp in list(self.active()):
            self.progress_experiment(exp)
        self.start_evaluations()
        self.progress_coordinator()
        if (
            self.expired()
            and not self.active()
            and self.state["coordinator"]["stage"] != "agent"
            and (
                not self.state["best"]
                or self.state["coordinator"]["seen_revision"] >= self.state["revision"]
            )
        ):
            self.state["status"] = "completed"
            self.save()
            return False
        self.save()
        if self.clock() - self.last_status >= self.config["status_interval"]:
            self.print_status()
        return True

    def print_status(self):
        self.last_status = self.clock()
        waiting = [
            e
            for e in [*self.active(), self.state["coordinator"]]
            if e.get("retry_at", 0) > self.clock()
        ]
        recent = [e["id"] for e in self.state["experiments"].values() if e["stage"] in TERMINAL][
            -5:
        ]
        print(
            json.dumps(
                {
                    "run": self.state["run_id"],
                    "best": self.state["best"],
                    "recent": recent,
                    "active": {
                        e["id"]: e["stage"] + ":" + e.get("observed", "") for e in self.active()
                    },
                    "coordinator": self.state["coordinator"]["stage"],
                    "scheduler_retry": self.state.get("scheduler_retry"),
                    "waiting": [
                        {
                            "id": e.get("id", "coordinator"),
                            "reason": e.get("error"),
                            "retry_at": e["retry_at"],
                        }
                        for e in waiting
                    ],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    async def run(self):
        self.state["status"] = "running"
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, setattr, self, "stop_requested", True)
        attempt = 0
        retry_at = 0
        try:
            while not self.stop_requested:
                if self.clock() >= retry_at:
                    try:
                        if not self.tick():
                            break
                        attempt = 0
                        self.state.pop("scheduler_retry", None)
                    except Exception as exc:
                        attempt += 1
                        retry_at = self.clock() + retry_delay(str(exc), attempt)
                        self.state["scheduler_retry"] = {"error": str(exc), "retry_at": retry_at}
                        # Disk-full may prevent persistence, but must remain observable.
                        print(
                            f"campaign retry at {retry_at}: {type(exc).__name__}: {exc}", flush=True
                        )
                        try:
                            self.save()
                        except OSError:
                            pass
                if self.clock() - self.last_status >= self.config["status_interval"]:
                    self.print_status()
                await asyncio.sleep(0.25)
        finally:
            if self.stop_requested:
                self.jobs.stop(
                    [
                        self.job_path(e)
                        for e in [*self.active(), self.state["coordinator"]]
                        if e.get("job")
                    ]
                )
                self.state["status"] = "stopped"
                self.save()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)


def inner(path: Path):
    with lock(path / "scheduler.lock"):
        atomic_json(path / "scheduler.json", identity())
        asyncio.run(Campaign(path).run())
