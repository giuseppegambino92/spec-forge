"""Split a restaurant bill between parties (integer cents)."""


def split_payment(total_cents: int, parties: int) -> list[int]:
    """Split ``total_cents`` into ``parties`` near-equal integer shares.

    :requires: 0 <= total_cents <= 10**9
    :requires: 1 <= parties <= 50
    :ensures: len(result) == parties
    :ensures: sum(result) == total_cents
    :ensures: max(result) - min(result) <= 1
    """
    base, extra = divmod(total_cents, parties)
    return [base + 1 if i < extra else base for i in range(parties)]
