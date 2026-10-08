# Minimal Autonomous Research Loop

A small Python reference for unattended, numerically evaluated research. Codex workers
edit isolated Git worktrees; a coordinator learns from results and proposes new
experiments, including combinations. The runner evaluates changes and advances
`research/<run-id>` only on strict improvements.

Requires Python 3.11+, Git, [uv](https://docs.astral.sh/uv/), and macOS or Linux (WSL on Windows).

## Quick start

Run from this repository:

```bash
uv sync
uv run pytest

# Full simulation: scripted models, real Git, evaluation, and crash recovery.
uv run python examples/example-linear-regression/simulate.py /tmp/research-simulation
```

For real model calls, authenticate Codex first using file-based credentials at
`$CODEX_HOME/auth.json` or `~/.codex/auth.json`. Both configured models must be available
to your account. The runner copies credentials into private task directories and never
starts a login flow. See the [Codex SDK documentation](https://learn.chatgpt.com/docs/codex-sdk).

```bash
# Use a new destination directory.
uv run python examples/example-linear-regression/prepare.py /tmp/my-research
uv run research run --config /tmp/my-research/research.toml --duration 12h

# Resume the run directory printed at startup.
uv run research resume /tmp/my-research/.research/<run-id>
```

See [example-linear-regression](examples/example-linear-regression/README.md) for the CPU example.

## Configure

Edit [research.toml](research.toml), [program.md](program.md), and the [role prompts](prompts/).
Paths in the config are relative to that file. The target repository needs an initial
commit; uncommitted checkout changes are excluded. Each run freezes its configuration,
instructions, and starting commit. Start a new run to apply changes.

- Models: coordinator `gpt-6.1-sol`, worker `gpt-6-luna`; independently configurable, with no automatic substitution.
- Concurrency: three experiments and one official evaluation by default.
- Scope: `allowed_paths` and `protected_paths` are literal files or directory prefixes.
  Commit and protect the evaluator's dependencies and data too.
- Evaluation: `evaluation_command` is an argv array run in a clean candidate worktree,
  with stdin closed. Its final stdout line must contain a finite score and boolean result:

```json
{"score": 0.42, "passed": true}
```

Set `direction` to `minimize` or `maximize`. Failed evaluations and ties never replace
the best candidate. Combinations are evaluated as separate experiments.

## Run and recover

Omit `--duration` to run indefinitely; values such as `12h` and `3d` stop new assignments
and drain existing work. There are no default timeouts or stagnation limits. Optional
`worker_timeout` and `evaluation_timeout` values are in seconds. Ctrl-C or SIGTERM stops
managed tasks without an automatic restart.

Status prints every 60 seconds. State, proposals, results, and logs live under
`<target>/.research/<run-id>/`. Inspect the best result with `git show research/<run-id>`;
avoid checking out that branch while the campaign is running.

The supervisor restarts crashed schedulers, recovers saved results, and observes live
jobs without duplicating them. Transient errors retry with backoff; persistent access
or resource errors retry less frequently. Quiet jobs are not assumed stuck. Use an OS
process manager with the `resume` command for recovery after a reboot.

Workers use a workspace sandbox with network access; approval requests are denied.
Evaluators run as ordinary local subprocesses. This reference assumes trusted research
code, not hostile programs. Keep campaign directories private: they contain credential
copies, and retained worktrees and logs consume disk space.

Inspired by [autoresearch](https://github.com/karpathy/autoresearch) and
[evo](https://github.com/evo-hq/evo). Uses the pinned `openai-codex` SDK; no database or
orchestration framework.
