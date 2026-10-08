# Linear regression

Fit slope and offset to minimize mean squared error with a deterministic CPU evaluator.
Only `candidate.py` is editable; `evaluate.py` stays fixed.

Run from the repository root, using a new destination:

```bash
# No model calls: explore, combine, reject a worse result, and recover from a crash.
uv run python examples/example-linear-regression/simulate.py /tmp/regression-demo

# Real models: authenticate Codex first, then prepare and run.
uv run python examples/example-linear-regression/prepare.py /tmp/regression-run
uv run research run --config /tmp/regression-run/research.toml --duration 12h
```

The simulation ends with `PASS` and a best score of `0`. See the [main README](../../README.md)
for configuration and recovery.
