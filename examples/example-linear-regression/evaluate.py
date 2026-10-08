"""Small, deterministic CPU regression objective; no third-party packages."""

import json
import math

from candidate import predict

values = [i / 50 for i in range(-100, 101)]
targets = [2 * x + 3 for x in values]
predictions = predict(values)
passed = len(predictions) == len(targets) and all(
    type(p) in (int, float) and math.isfinite(p) for p in predictions
)
score = sum((p - y) ** 2 for p, y in zip(predictions, targets)) / len(targets) if passed else 1e30
print(json.dumps({"score": score, "passed": passed, "metric": "mean_squared_error"}))
