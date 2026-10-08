from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .campaign import create, inner
from .config import duration
from .jobs import Jobs, boot_id, processes
from .storage import atomic_json, lock, read_json, retry_delay


def supervise(path: Path, *, inner_command: list[str] | None = None) -> int:
    path = path.resolve()
    if not (path / "state.json").exists():
        raise ValueError(f"not a campaign directory: {path}")
    with lock(path / "campaign.lock"):
        stopping = False
        child = None

        def signal_saved_scheduler(sig):
            if not (path / "scheduler.json").exists():
                return
            owner = read_json(path / "scheduler.json")
            current = processes().get(owner["pid"])
            if (
                owner["boot"] == boot_id()
                and current
                and all(current[k] == owner[k] for k in ("started", "command"))
            ):
                try:
                    os.kill(owner["pid"], sig)
                except ProcessLookupError:
                    pass

        def stop(_signum, _frame):
            nonlocal stopping
            stopping = True
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
            else:
                signal_saved_scheduler(signal.SIGTERM)

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        failures = 0
        observed_existing = False
        last_observation = 0.0
        try:
            while not stopping:
                status = read_json(path / "state.json")["status"]
                if status == "completed" or (observed_existing and status == "stopped"):
                    return 0
                try:
                    with lock(path / "scheduler.lock"):
                        pass
                except BlockingIOError:
                    # A previous supervisor can die while its scheduler survives.
                    # Observe the existing writer instead of spawning competitors.
                    observed_existing = True
                    if time.time() - last_observation >= 60:
                        print("existing scheduler is alive; observing saved campaign", flush=True)
                        last_observation = time.time()
                    time.sleep(0.25)
                    continue
                child = subprocess.Popen(
                    (inner_command or [sys.executable, "-m", "research_loop.cli", "_inner"])
                    + [str(path)],
                    stdin=subprocess.DEVNULL,
                )
                atomic_json(
                    path / "supervisor.json",
                    {"pid": os.getpid(), "scheduler_pid": child.pid, "restarts": failures},
                )
                stop_deadline = None
                while child.poll() is None:
                    if stopping:
                        stop_deadline = stop_deadline or time.monotonic() + 3
                        if time.monotonic() >= stop_deadline:
                            child.kill()
                    time.sleep(0.05)
                code = child.wait()
                if stopping or code in (0, -signal.SIGTERM, -signal.SIGINT, 130, 143):
                    return 0
                failures += 1
                delay = retry_delay(f"scheduler exit {code}", failures)
                retry_at = time.time() + delay
                atomic_json(
                    path / "supervisor.json",
                    {
                        "pid": os.getpid(),
                        "exit_code": code,
                        "restarts": failures,
                        "retry_at": retry_at,
                    },
                )
                print(f"scheduler exited {code}; resume at {time.ctime(retry_at)}", flush=True)
                while not stopping and time.time() < retry_at:
                    time.sleep(min(0.25, max(0, retry_at - time.time())))
            return 0
        finally:
            # Includes an explicit signal during the supervisor's backoff window.
            if stopping:
                deadline = time.monotonic() + 3
                while True:
                    try:
                        with lock(path / "scheduler.lock"):
                            Jobs().stop(list((path / "jobs").glob("*/*")))
                            state = read_json(path / "state.json")
                            state["status"] = "stopped"
                            atomic_json(path / "state.json", state)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            signal_saved_scheduler(signal.SIGKILL)
                        time.sleep(0.05)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Unattended numerical research with Codex")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{run,resume}")
    start = sub.add_parser("run", help="start a campaign")
    start.add_argument("--config", type=Path, default=Path("research.toml"))
    start.add_argument("--duration", type=duration)
    resume = sub.add_parser("resume", help="resume a campaign using its frozen contract")
    resume.add_argument("run_dir", type=Path)
    internal = sub.add_parser("_inner")
    internal.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "_inner":
            inner(args.run_dir.resolve())
            return
        path = (
            create(args.config, args.duration) if args.command == "run" else args.run_dir.resolve()
        )
        print(f"campaign: {path}", flush=True)
        return supervise(path)
    except BlockingIOError:
        parser.exit(2, "campaign already has an active supervisor or scheduler\n")
    except (ValueError, KeyError, FileNotFoundError) as exc:
        parser.exit(2, f"research: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
