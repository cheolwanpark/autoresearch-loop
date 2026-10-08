SLOPE = 0.0
OFFSET = 0.0


def predict(values):
    return [SLOPE * x + OFFSET for x in values]
