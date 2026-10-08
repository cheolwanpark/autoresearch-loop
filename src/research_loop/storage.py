"""Atomic JSON artifacts, advisory locks, and retry policy (POSIX)."""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path


def read_json(path: Path):
    return json.loads(path.read_text())


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(path: Path, blocking: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def retry_delay(error: str, attempt: int, retry_after=None, *, jitter=random.random) -> float:
    if retry_after is not None:
        try:
            delay = float(retry_after)
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (TypeError, ValueError):
            pass
    # The SDK sometimes carries provider retry hints only in an error message.
    hint = re.search(r"(?:retry.after|try again in)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*s", error, re.I)
    if hint:
        return float(hint[1])
    if re.search(
        r"auth|unauthorized|forbidden|permission|access.denied|model.*(?:not found|not available|not supported|does not exist|access)|401|403|ENOSPC|EACCES|no space|disk.full|quota|insufficient",
        error,
        re.I,
    ):
        return 1800.0
    return min(300.0, 5.0 * 2 ** min(max(0, attempt - 1), 6) * (1 + jitter() * 0.2))


def error_info(exc: Exception) -> dict:
    data = getattr(exc, "data", None)
    retry_after = data.get("retry_after") if isinstance(data, dict) else None
    return {
        "error": f"{type(exc).__name__}: {exc}",
        "retry_after": retry_after,
        "persistent": isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EACCES),
    }


def timestamp() -> float:
    return time.time()
