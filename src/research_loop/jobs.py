"""Detached jobs survive scheduler crashes; lock + process identity prevent duplicates."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .storage import atomic_json, lock, read_json


def processes() -> dict[int, dict]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,lstart=,stat=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    found = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 8)
        if len(parts) == 9:
            found[int(parts[0])] = {
                "pgid": int(parts[1]),
                "started": " ".join(parts[2:7]),
                "status": parts[7],
                "command": parts[8],
            }
    return found


def boot_id() -> str:
    linux = Path("/proc/sys/kernel/random/boot_id")
    if linux.exists():
        return linux.read_text().strip()
    return subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True).strip()


def identity() -> dict:
    pid = os.getpid()
    return {"pid": pid, "boot": boot_id(), **processes()[pid]}


def job_status(path: Path) -> str:
    if (path / "result.json").exists():
        return "done"
    try:
        with lock(path / "job.lock"):
            pass
    except BlockingIOError:
        return "running"
    if not (path / "owner.json").exists():
        return "new"
    owner = read_json(path / "owner.json")
    if owner["boot"] != boot_id():
        return "dead"
    table = processes()
    current = table.get(owner["pid"])
    if (
        current
        and "Z" not in current["status"]
        and all(current[k] == owner[k] for k in ("started", "command", "pgid"))
    ):
        return "running"
    # The wrapper can die while its app-server/evaluator children still run.
    # Never start a replacement until the group disappears. PID reuse is not proof.
    if any(p["pgid"] == owner["pgid"] and "Z" not in p["status"] for p in table.values()):
        return "uncertain"
    return "dead"


class Jobs:
    def __init__(self):
        self.children: list[subprocess.Popen] = []
        self.child_paths: dict[int, Path] = {}

    def launch(self, path: Path, request: dict) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if not (path / "request.json").exists():
            atomic_json(path / "request.json", request)
        if job_status(path) != "new":
            return
        # A second launch in the tiny pre-lock window is harmless: the wrapper's
        # nonblocking lock and result check allow only one actual execution.
        with (path / "wrapper.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "research_loop.job_runner", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        self.children.append(child)
        self.child_paths[child.pid] = path

    def reap(self):
        self.children[:] = [p for p in self.children if p.poll() is None]
        live = {p.pid for p in self.children}
        self.child_paths = {pid: path for pid, path in self.child_paths.items() if pid in live}

    def stop(self, paths: list[Path]) -> None:
        groups = []
        # Popen handles prove ownership even before a new wrapper saves its ID.
        for child in self.children:
            if self.child_paths[child.pid] in paths and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    groups.append(child.pid)
                except ProcessLookupError:
                    pass
        table = processes()
        for path in paths:
            if not (path / "owner.json").exists():
                continue
            owner = read_json(path / "owner.json")
            current = table.get(owner["pid"])
            if (
                owner["pgid"] not in groups
                and owner["boot"] == boot_id()
                and current
                and "Z" not in current["status"]
                and all(current[k] == owner[k] for k in ("started", "command", "pgid"))
            ):
                try:
                    os.killpg(owner["pgid"], signal.SIGTERM)
                    groups.append(owner["pgid"])
                except (ProcessLookupError, PermissionError):
                    # macOS may report EPERM for a group that just became zombies.
                    if any(
                        p["pgid"] == owner["pgid"] and "Z" not in p["status"]
                        for p in processes().values()
                    ):
                        raise
        # Explicit stop (or configured timeout), never silence, authorizes cleanup.
        deadline = time.monotonic() + 2
        while groups and time.monotonic() < deadline:
            self.reap()
            table = processes()
            groups = [
                g
                for g in groups
                if any(p["pgid"] == g and "Z" not in p["status"] for p in table.values())
            ]
            if groups:
                time.sleep(0.05)
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                if any(p["pgid"] == group and "Z" not in p["status"] for p in processes().values()):
                    raise
        self.reap()
