"""Run-length codec for compact telemetry frames."""


def rle_encode(data: str) -> list[tuple[str, int]]:
    """Encode ``data`` as maximal (char, run length) pairs.

    :ensures: all(n >= 1 for _, n in result)
    """
    runs = []
    i = 0
    while i < len(data):
        j = i
        while j + 1 < len(data) and data[j + 1] == data[i]:
            j += 1
        runs.append((data[i], j - i))
        i = j + 1
    return runs


def rle_decode(runs: list[tuple[str, int]]) -> str:
    """Inverse of :func:`rle_encode`. Frames carry at most 4096 repeats per run.

    :requires: all(0 <= n <= 4096 for _, n in runs)
    """
    return "".join(ch * n for ch, n in runs)
