"""Benchmark corpus: 12 subtle production bug classes, each with a reference fix and a naive unit suite."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class BugCase:
    id: str
    module: str
    bug_class: str
    domain: str
    description: str
    oracle: str = "equal"            # equal | multiset | atomicity  (how patched output is compared to reference)
    targets: tuple[str, ...] = field(default_factory=tuple)

    @property
    def buggy_path(self) -> Path:
        return ROOT / "buggy" / f"{self.module}.py"

    @property
    def reference_path(self) -> Path:
        return ROOT / "reference" / f"{self.module}.py"

    def buggy_source(self) -> str:
        return self.buggy_path.read_text(encoding="utf-8")

    def reference_source(self) -> str:
        return self.reference_path.read_text(encoding="utf-8")


CASES: list[BugCase] = [
    BugCase("B01", "b01_split_payment", "conservation leak (rounding)", "financial",
            "round(total/n) shares lose or invent cents", oracle="multiset"),
    BugCase("B02", "b02_sliding_window", "off-by-one window boundary", "rate limiting",
            "eviction uses >= so the boundary event leaves the closed window early: limit+1 admitted"),
    BugCase("B03", "b03_normalize_whitespace", "non-idempotent normalisation", "text/search",
            "single-pass replace('  ', ' ') leaves runs of 3+ spaces partially collapsed"),
    BugCase("B04", "b04_mutable_default", "mutable default state corruption", "state",
            "registry=[] default is shared across calls and the caller's list is mutated"),
    BugCase("B05", "b05_zscore", "division by zero on degenerate variance", "analytics",
            "constant series -> pstdev == 0 -> ZeroDivisionError"),
    BugCase("B06", "b06_quota_underflow", "integer underflow in quota deduction", "rate limiting",
            "remaining - cost goes negative for an unsigned 32-bit counter"),
    BugCase("B07", "b07_ledger_total", "floating-point drift (order dependence)", "financial",
            "naive float summation gives different balances for permutations of the same postings"),
    BugCase("B08", "b08_paginate", "off-by-one slice bound", "API",
            "end = len(items) - 1 drops the last element of the last page"),
    BugCase("B09", "b09_rle_codec", "round-trip invertibility violation", "serialization",
            "run length recorded as j - i instead of j - i + 1"),
    BugCase("B10", "b10_top_k", "input aliasing / mutation", "state",
            "scores.sort() reorders the caller's list in place"),
    BugCase("B11", "b11_hit_counter", "race condition (lost update)", "concurrency",
            "read-modify-write of self.hits without a lock", oracle="atomicity"),
    BugCase("B12", "b12_percentile", "index out of bounds at p = 100", "analytics",
            "rank = int(p/100*n) == n for the maximum percentile"),
]
