"""Latency SLO reporting."""


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile of ``values``.

    :requires: 1 <= len(values) <= 40
    :requires: all(math.isfinite(v) for v in values)
    :requires: 0 <= p <= 100
    :ensures: min(values) <= result <= max(values)
    """
    ordered = sorted(values)
    rank = int(p / 100 * len(ordered))
    return ordered[rank]
