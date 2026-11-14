"""Leaderboard helper."""


def top_k(scores: list[int], k: int) -> list[int]:
    """Return the ``k`` largest scores in descending order.

    :requires: 0 <= k <= 50
    :ensures: len(result) == min(k, len(scores))
    :ensures: result == sorted(result, reverse=True)
    """
    scores.sort(reverse=True)
    return scores[:k]
