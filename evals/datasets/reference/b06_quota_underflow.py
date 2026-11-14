"""API quota accounting (counters persisted as unsigned 32-bit integers)."""


def deduct_quota(remaining: int, cost: int) -> int:
    """Deduct ``cost`` units from an unsigned 32-bit quota counter.

    :requires: 0 <= remaining <= 2**32 - 1
    :requires: 0 <= cost <= 2**32 - 1
    :ensures: 0 <= result <= remaining
    """
    return max(0, remaining - cost)
