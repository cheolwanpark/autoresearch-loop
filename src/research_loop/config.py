from __future__ import annotations

import math
import re
import tomllib
from pathlib import Path, PurePosixPath

DEFAULTS = {
    "coordinator_model": "gpt-6.1-sol",
    "worker_model": "gpt-6-luna",
    "workers": 3,
    "evaluation_concurrency": 1,
    "direction": "minimize",
    "worker_timeout": None,
    "evaluation_timeout": None,
    "status_interval": 60,
    "protected_paths": [],
}


def duration(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h|d)", value)
    if not match or not math.isfinite(float(match[1])) or float(match[1]) <= 0:
        raise ValueError("duration must be positive, e.g. 12h or 3d")
    return float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def load_config(path: Path) -> dict:
    path = path.resolve()
    raw = tomllib.loads(path.read_text())
    unknown = (
        set(raw)
        - set(DEFAULTS)
        - {
            "repository",
            "program",
            "coordinator_instructions",
            "worker_instructions",
            "allowed_paths",
            "evaluation_command",
        }
    )
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    config = DEFAULTS | raw
    for key in ("repository", "program", "coordinator_instructions", "worker_instructions"):
        config[key] = str((path.parent / config[key]).resolve())
    for key in ("workers", "evaluation_concurrency"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("worker_timeout", "evaluation_timeout", "status_interval"):
        value = config[key]
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        ):
            raise ValueError(f"{key} must be positive or omitted")
    if config["direction"] not in ("minimize", "maximize"):
        raise ValueError("direction must be minimize or maximize")
    for key in ("coordinator_model", "worker_model"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key} must be a model name")
    for key in ("allowed_paths", "evaluation_command"):
        if (
            not isinstance(config.get(key), list)
            or not config[key]
            or not all(isinstance(s, str) and s for s in config[key])
        ):
            raise ValueError(f"{key} must be a nonempty string array")
    if not isinstance(config["protected_paths"], list) or not all(
        isinstance(p, str) and p for p in config["protected_paths"]
    ):
        raise ValueError("protected_paths must be a string array")
    for value in config["allowed_paths"] + config["protected_paths"]:
        p = PurePosixPath(value)
        if (
            p.is_absolute()
            or ".." in p.parts
            or not p.parts
            or any(x in p.parts for x in (".git", ".research"))
        ):
            raise ValueError(f"unsafe allowed path: {value}")
        if value != p.as_posix() and value != p.as_posix() + "/":
            raise ValueError(f"use a normalized relative path: {value}")
        if any(c in value for c in "*?["):
            raise ValueError("allowed_paths are literal files or directory prefixes, not globs")
    return config
