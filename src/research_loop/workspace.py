"""Git mutations belong to the scheduler, never the model."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def environment(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    cache = cwd / ".research-local"
    cache.mkdir(exist_ok=True)
    for name in ("tmp", "cache"):
        (cache / name).mkdir(exist_ok=True)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="/usr/bin/false",
        SSH_ASKPASS="/usr/bin/false",
        GIT_EDITOR="true",
        GIT_SEQUENCE_EDITOR="true",
        GIT_CONFIG_NOSYSTEM="1",
        TMPDIR=str(cache / "tmp"),
        TMP=str(cache / "tmp"),
        TEMP=str(cache / "tmp"),
        XDG_CACHE_HOME=str(cache / "cache"),
        UV_CACHE_DIR=str(cache / "cache" / "uv"),
        PYTHONPYCACHEPREFIX=str(cache / "cache" / "pycache"),
        PYTHONUNBUFFERED="1",
    )
    return env


def git(repo: Path, *args: str, input: bytes | None = None) -> str:
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/usr/bin/false", GIT_EDITOR="true")
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Research Loop",
            "-c",
            "user.email=research@localhost",
            *args,
        ],
        cwd=repo,
        env=env,
        input=input,
        stdin=subprocess.DEVNULL if input is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]}: {result.stderr.decode(errors='replace')}")
    return result.stdout.decode(errors="surrogateescape").strip("\n")


def ignore_runs(repo: Path) -> None:
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = repo / exclude
    existing = exclude.read_text() if exclude.exists() else ""
    lines = ["/.research/", "/.research-local/"]
    with exclude.open("a") as stream:
        for line in lines:
            if line not in existing.splitlines():
                stream.write("\n" + line + "\n")


def worktree(repo: Path, path: Path, commit: str) -> None:
    if (path / ".git").exists():
        return
    git(repo, "worktree", "add", "--detach", str(path), commit)


def allowed(name: str, paths: list[str]) -> bool:
    return any(name == p.rstrip("/") or name.startswith(p.rstrip("/") + "/") for p in paths)


def changes(path: Path, base: str) -> list[str]:
    tracked = git(path, "diff", "--name-only", "-z", "--no-renames", base, "--").split("\0")
    untracked = git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted(set(filter(None, tracked + untracked)))


def violations(path: Path, base: str, paths: list[str], protected: list[str] = ()) -> list[str]:
    invalid = []
    for name in changes(path, base):
        file = path / name
        # No symlinks in candidates: they can redirect imports or future writes.
        if (
            not allowed(name, paths)
            or allowed(name, protected)
            or file.is_symlink()
            or not file.resolve().is_relative_to(path.resolve())
        ):
            invalid.append(name)
    return invalid


def commit_candidate(
    path: Path, base: str, message: str, paths: list[str], protected: list[str] = ()
) -> str:
    invalid = violations(path, base, paths, protected)
    if invalid:
        raise ValueError(f"outside allowed paths or symlinks: {invalid}")
    if git(path, "rev-parse", "HEAD") != base:
        # The only legal existing commit is our own commit from a crash window.
        if (
            git(path, "log", "-1", "--format=%B").strip() == message
            and git(path, "rev-parse", "HEAD^") == base
        ):
            return git(path, "rev-parse", "HEAD")
        raise ValueError("worker changed Git HEAD; runner owns Git history")
    # An agent may have staged a disallowed file then removed it. Reset the index.
    git(path, "reset", "--mixed", base)
    names = changes(path, base)
    if names:
        git(path, "add", "--", *names)
    git(path, "commit", "--allow-empty", "-m", message)
    return git(path, "rev-parse", "HEAD")


def ref(repo: Path, name: str) -> str | None:
    try:
        return git(repo, "rev-parse", "--verify", name)
    except RuntimeError:
        return None


def set_ref(repo: Path, name: str, commit: str) -> None:
    current = ref(repo, name)
    if current != commit:
        git(repo, "update-ref", name, commit, current or "0" * len(commit))
