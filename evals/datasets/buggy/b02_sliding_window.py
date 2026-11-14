"""Sliding-window rate limiter used in front of the payments API."""

from collections import deque


def admit_requests(timestamps: list[int], limit: int, window: int) -> list[bool]:
    """Decide admission for each request timestamp (seconds, non-decreasing).

    A request at time t is admitted iff fewer than ``limit`` admitted requests lie
    in the closed window [t - window, t].

    :requires: timestamps == sorted(timestamps)
    :requires: all(0 <= t <= 100 for t in timestamps)
    :requires: 1 <= limit <= 5
    :requires: 1 <= window <= 10
    :ensures: len(result) == len(timestamps)
    :ensures: all(sum(1 for j in range(len(timestamps)) if result[j] and timestamps[i] - window <= timestamps[j] <= timestamps[i]) <= limit for i in range(len(timestamps)))
    """
    admitted = deque()
    decisions = []
    for t in timestamps:
        while admitted and t - admitted[0] >= window:
            admitted.popleft()
        if len(admitted) < limit:
            admitted.append(t)
            decisions.append(True)
        else:
            decisions.append(False)
    return decisions
