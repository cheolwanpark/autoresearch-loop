"""Paid calls are replaced by a scripted model. Git, evaluation and recovery are real.

uv run python examples/example-linear-regression/simulate.py /tmp/research-simulation
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from prepare import prepare
from research_loop.campaign import Campaign, create
from research_loop.cli import supervise
from research_loop.jobs import Jobs
from research_loop.storage import atomic_json, read_json
from research_loop.workspace import git


class ScriptedModels(Jobs):
    def launch(self, path, request):
        if request["kind"] == "evaluation":
            return super().launch(path, request)
        if (path / "result.json").exists():
            return
        prompt = json.loads(request["prompt"])
        if request["role"] == "worker":
            hypothesis = prompt["hypothesis"]
            coefficients = {
                "slope": (2, 0),
                "offset": (0, 3),
                "combine": (2, 3),
                "bad-combine": (4, 6),
            }
            slope, offset = coefficients[hypothesis]
            (Path(request["cwd"]) / "candidate.py").write_text(
                f"SLOPE = {slope}\nOFFSET = {offset}\n\ndef predict(values):\n    return [SLOPE * x + OFFSET for x in values]\n"
            )
            text = f"Scripted {hypothesis} modification"
        else:
            recent = {e["hypothesis"]: e for e in prompt["recent"]}
            tasks = []
            if len(recent) == 1:
                tasks = [proposal("slope", "baseline"), proposal("offset", "baseline")]
            elif all(recent.get(name, {}).get("stage") == "done" for name in ("slope", "offset")):
                if "combine" not in recent:
                    tasks = [
                        proposal(
                            "combine",
                            prompt["best"],
                            [recent["slope"]["id"], recent["offset"]["id"]],
                        )
                    ]
                elif recent["combine"]["stage"] == "done" and "bad-combine" not in recent:
                    tasks = [
                        proposal(
                            "bad-combine",
                            prompt["best"],
                            [recent["slope"]["id"], recent["offset"]["id"]],
                        )
                    ]
            text = json.dumps(
                {
                    "summary": "Slope and offset improve separate errors; combinations require their own evaluation. Retain the best measured score.",
                    "tasks": tasks[: prompt["slots"]],
                }
            )
        atomic_json(
            path / "result.json",
            {"ok": True, "thread_id": "scripted-" + path.parent.name, "text": text},
        )


def proposal(name, base, combine=None):
    return {
        "hypothesis": name,
        "base": base,
        "instructions": "Apply the scripted change",
        "combine": combine or [],
    }


class Simulation(Campaign):
    def tick(self):
        baseline = self.state["experiments"]["baseline"]
        if baseline["stage"] == "eval" and not (self.path / "simulated-crash").exists():
            # Abrupt scheduler death after launch. The wrapper owns the evaluation.
            if (self.job_path(baseline) / "owner.json").exists():
                (self.path / "simulated-crash").touch()
                print("SIMULATION: killing scheduler; supervisor will resume", flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
        bad = next(
            (e for e in self.state["experiments"].values() if e["hypothesis"] == "bad-combine"),
            None,
        )
        if bad and bad["stage"] == "done":
            self.state["deadline"] = self.clock()
        return super().tick()


def main():
    if sys.argv[1] == "--inner":
        asyncio.run(Simulation(Path(sys.argv[2]), jobs=ScriptedModels()).run())
        return
    destination = prepare(Path(sys.argv[1]))
    path = create(destination / "research.toml")
    print(f"Simulation campaign: {path}", flush=True)
    supervise(path, inner_command=[sys.executable, str(Path(__file__).resolve()), "--inner"])
    state = read_json(path / "state.json")
    best = state["experiments"][state["best"]]
    assert best["hypothesis"] == "combine" and best["evaluation"]["score"] == 0
    assert git(destination, "rev-parse", state["branch"]) == best["commit"]
    assert len(state["experiments"]["baseline"]["jobs"]) == 1
    assert read_json(path / "supervisor.json")["restarts"] == 1
    for exp in state["experiments"].values():
        print(f"{exp['id']}: {exp['hypothesis']}: {exp.get('evaluation')}")
    print(
        f"PASS: crash recovery, independent branches, combination re-evaluation, best branch = {state['branch']}"
    )


if __name__ == "__main__":
    main()
