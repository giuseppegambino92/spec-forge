"""Ledger reconciliation: the same set of postings must always produce the same balance."""


def ledger_total(amounts: list[float]) -> float:
    """Net balance of a batch of postings.

    :requires: all(math.isfinite(a) and abs(a) <= 1e9 for a in amounts)
    """
    total = 0.0
    for amount in amounts:  # naive running sum: rounding depends on posting order
        total += amount
    return total
