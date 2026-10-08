"""Create a committed, isolated example repository. Never overwrites a directory."""

import argparse
import shutil
import subprocess
from pathlib import Path


def prepare(destination: Path) -> Path:
    example = Path(__file__).resolve().parent
    root = example.parents[1]
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("candidate.py", "evaluate.py"):
        shutil.copy2(example / name, destination / name)
    shutil.copy2(root / "program.md", destination / "program.md")
    shutil.copytree(root / "prompts", destination / "prompts")
    shutil.copy2(root / "research.toml", destination / "research.toml")
    (destination / ".gitignore").write_text(".research/\n.research-local/\n__pycache__/\n")
    subprocess.run(["git", "init", "-q", str(destination)], check=True)
    subprocess.run(["git", "add", "."], cwd=destination, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Research Example",
            "-c",
            "user.email=research@localhost",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial example",
        ],
        cwd=destination,
        check=True,
    )
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    print(prepare(parser.parse_args().destination))
