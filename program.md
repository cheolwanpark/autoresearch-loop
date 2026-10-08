# Research program: fit a tiny regression model

Improve candidate.py's predict(values) model to minimize mean squared error on
the fixed evaluator. This deliberately small CPU example makes independent slope
and offset experiments and a subsequent combination easy to reproduce.

Only candidate.py is editable. evaluate.py is the fixed official evaluator. Keep
predict's interface: accept a list of numbers and return a list of predictions of
the same length. Do not read validation targets from the evaluator, change the
score protocol, detect evaluator execution, or cache answers for benchmark inputs.
Use the known research hypothesis that a linear model with slope near 2 and offset
near 3 should improve the initial constant-zero predictions.

Explore slope and offset separately first, use the recorded errors, then evaluate
promising combinations. Subsequent experiments can explore other implementations
or model classes. Individual failures and lack of improvement are not stop criteria.
The campaign continues until the runner's duration expires or an operator stops it.
