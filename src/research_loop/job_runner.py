"""One recoverable agent turn or evaluation. Launched in its own process group."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

from .jobs import identity
from .storage import atomic_json, error_info, lock, read_json, timestamp
from .workspace import environment


def parse_score(stdout: str) -> dict:
    lines = stdout.rstrip().splitlines()
    if not lines:
        raise ValueError("evaluator did not print a result")
    result = json.loads(lines[-1])
    if (
        not isinstance(result, dict)
        or type(result.get("score")) not in (int, float)
        or not math.isfinite(result["score"])
    ):
        raise ValueError("score must be a finite number")
    if type(result.get("passed")) is not bool:
        raise ValueError("passed must be a JSON boolean")
    # Diagnostic metrics must also be valid JSON, without NaN/Infinity.
    json.dumps(result, allow_nan=False)
    return result


def evaluate(request: dict, path: Path) -> dict:
    cwd = Path(request["cwd"])
    with (path / "stdout.log").open("wb") as stdout, (path / "stderr.log").open("wb") as stderr:
        child = subprocess.Popen(
            request["command"],
            cwd=cwd,
            env=environment(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
        code = child.wait()
    if code:
        return {
            "ok": True,
            "evaluation": {"passed": False, "score": None},
            "exit_code": code,
            "error": f"evaluation exited {code}",
        }
    try:
        # Only the final line is the scoring protocol; stdout remains on disk.
        with (path / "stdout.log").open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 1024 * 1024))
            tail = stream.read().decode(errors="replace")
        return {"ok": True, "evaluation": parse_score(tail), "exit_code": code}
    except (ValueError, TypeError) as exc:
        return {
            "ok": True,
            "evaluation": {"passed": False, "score": None},
            "exit_code": code,
            "error": str(exc),
        }


def main() -> None:
    path = Path(sys.argv[1]).resolve()
    try:
        with lock(path / "job.lock"):
            if (path / "result.json").exists():
                return
            atomic_json(path / "owner.json", identity())
            request = read_json(path / "request.json")
            started = timestamp()
            try:
                from .agent import run_agent

                result = (
                    evaluate(request, path)
                    if request["kind"] == "evaluation"
                    else {"ok": True, **run_agent(request, path)}
                )
            except Exception as exc:
                result = {"ok": False, **error_info(exc)}
            atomic_json(
                path / "result.json", result | {"started": started, "finished": timestamp()}
            )
    except BlockingIOError:
        return


if __name__ == "__main__":
    main()
