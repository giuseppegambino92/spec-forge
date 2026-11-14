"""Process-wide request counter shared by the worker threads of the HTTP server."""

import time


class HitCounter:
    """Counts hits from many worker threads concurrently."""

    def __init__(self) -> None:
        self.hits = 0

    def increment(self, n: int = 1) -> None:
        current = self.hits
        time.sleep(0)  # metrics flush hook (I/O) between read and write
        self.hits = current + n
