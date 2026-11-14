"""Anomaly scoring for latency series."""

import statistics


def zscores(samples: list[float]) -> list[float]:
    """Standardise a series. A constant series has no spread: every z-score is 0.0.

    :requires: 1 <= len(samples)
    :requires: all(abs(x) <= 1e6 for x in samples)
    :ensures: len(result) == len(samples)
    """
    mu = statistics.fmean(samples)
    sigma = statistics.pstdev(samples)
    return [(x - mu) / sigma for x in samples]
