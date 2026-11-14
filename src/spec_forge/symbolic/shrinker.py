"""Hierarchical delta-debugging input shrinker.

Given a failing case (dict param -> value) and a predicate ``still_fails(case) -> bool``,
find a locally minimal case under the structural ``complexity`` order:

  level 1  per parameter, structural ddmin on containers (lists, strings, dicts, sets, tuples):
           remove chunks n/2, n/4, ... 1 (Zeller & Hildebrandt's ddmin complement steps)
  level 2  recursive descent into each element / value
  level 3  scalar simplification: ints by binary search toward 0, floats toward short
           integral values, strings toward 'a'/' ' characters, bools to False

Passes repeat until a fixpoint, a predicate-evaluation budget, or a wall-clock budget.
Every accepted candidate strictly lowers complexity, so termination is guaranteed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from ..codec import case_complexity, complexity


@dataclass
class ShrinkResult:
    minimal: dict[str, Any]
    initial_complexity: int
    final_complexity: int
    evaluations: int
    elapsed_ms: float
    steps: list[int] = field(default_factory=list)   # complexity after each accepted step
    budget_exhausted: bool = False

    @property
    def reduction(self) -> float:
        if self.initial_complexity <= 0:
            return 0.0
        return 1.0 - self.final_complexity / self.initial_complexity


class _Budget(Exception):
    pass


class Shrinker:
    def __init__(self, predicate: Callable[[dict[str, Any]], bool], max_evaluations: int = 4000,
                 time_budget_ms: float = 250.0, frozen: frozenset[str] = frozenset()) -> None:
        self.predicate = predicate
        self.max_evaluations = max_evaluations
        self.deadline = 0.0
        self.time_budget_ms = time_budget_ms
        self.evaluations = 0
        self.frozen = frozen
        self._seen: set[str] = set()

    # ------------------------------------------------------------------ driver
    def shrink(self, case: dict[str, Any]) -> ShrinkResult:
        t0 = time.perf_counter()
        self.deadline = t0 + self.time_budget_ms / 1000.0
        initial = case_complexity(case)
        current = dict(case)
        steps = [initial]
        exhausted = False
        try:
            changed = True
            while changed:
                changed = False
                for key in sorted(current, key=lambda k: -complexity(current[k])):
                    if key in self.frozen:
                        continue
                    for cand in self._candidates(current[key]):
                        if _order(cand) >= _order(current[key]):
                            continue
                        trial = dict(current)
                        trial[key] = cand
                        if self._test(trial):
                            current = trial
                            steps.append(case_complexity(current))
                            changed = True
                            break  # restart candidate generation from the smaller value
                    if changed:
                        break
        except _Budget:
            exhausted = True
        return ShrinkResult(
            minimal=current, initial_complexity=initial, final_complexity=case_complexity(current),
            evaluations=self.evaluations, elapsed_ms=(time.perf_counter() - t0) * 1000.0, steps=steps,
            budget_exhausted=exhausted,
        )

    def _test(self, trial: dict[str, Any]) -> bool:
        key = repr(sorted(trial.items(), key=lambda kv: kv[0]))
        if key in self._seen:
            return False
        if self.evaluations >= self.max_evaluations or time.perf_counter() > self.deadline:
            raise _Budget()
        self._seen.add(key)
        self.evaluations += 1
        try:
            return bool(self.predicate(trial))
        except Exception:
            return False

    # ------------------------------------------------------------------ candidates (simplest first)
    def _candidates(self, v: Any) -> Iterator[Any]:
        if isinstance(v, bool):
            if v:
                yield False
        elif isinstance(v, int):
            yield from _int_candidates(v)
        elif isinstance(v, float):
            yield from _float_candidates(v)
        elif isinstance(v, str):
            yield from _seq_candidates(list(v), lambda xs: "".join(xs), self._char_candidates)
        elif isinstance(v, bytes):
            yield from _seq_candidates(list(v), bytes, _int_candidates)
        elif isinstance(v, list):
            if v and all(type(x) is int for x in v):
                m = min(v)
                if m > 0:
                    yield [x - m for x in v]          # translate toward the origin (keeps order & gaps)
                elif max(v) < 0:
                    yield [x - max(v) for x in v]
            yield from _seq_candidates(v, list, self._candidates)
        elif isinstance(v, tuple):
            # fixed-arity tuples keep their length; only elements shrink
            for i, x in enumerate(v):
                for c in self._candidates(x):
                    yield v[:i] + (c,) + v[i + 1:]
        elif isinstance(v, (set, frozenset)):
            items = sorted(v, key=repr)
            ctor = type(v)
            yield from _seq_candidates(items, lambda xs: ctor(xs), lambda x: iter(()))
        elif isinstance(v, dict):
            keys = list(v)
            for chunk in _chunks(len(keys)):
                for start in range(0, len(keys), chunk):
                    drop = set(keys[start:start + chunk])
                    yield {k: x for k, x in v.items() if k not in drop}
            for k in keys:
                for c in self._candidates(v[k]):
                    d = dict(v)
                    d[k] = c
                    yield d

    @staticmethod
    def _char_candidates(c: str) -> Iterator[str]:
        for simple in ("a", " ", "0"):
            if c != simple:
                yield simple


def _magnitude(v: Any) -> float:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return abs(v) if not (isinstance(v, float) and math.isnan(v)) else 1.0
    if isinstance(v, str):
        return float(sum(ord(c) for c in v))
    if isinstance(v, (list, tuple, set, frozenset)):
        return float(sum(_magnitude(x) for x in v))
    if isinstance(v, dict):
        return float(sum(_magnitude(x) for x in v.values()))
    return 0.0


def _order(v: Any) -> tuple[int, float]:
    """Strict well-founded shrink order: structural complexity, then magnitude."""
    return complexity(v), _magnitude(v)


def _chunks(n: int) -> list[int]:
    out, size = [], n
    while size >= 1:
        out.append(size)
        size //= 2
    return out


def _seq_candidates(xs: list[Any], build: Callable[[list[Any]], Any],
                    elem: Callable[[Any], Iterator[Any]]) -> Iterator[Any]:
    n = len(xs)
    if n == 0:
        return
    yield build([])
    # ddmin complements: drop contiguous chunks of decreasing size
    for chunk in _chunks(n):
        for start in range(0, n, chunk):
            rest = xs[:start] + xs[start + chunk:]
            if len(rest) < n:
                yield build(rest)
    # deduplicate runs (helps strings / sorted lists)
    for i in range(n):
        for c in elem(xs[i]):
            yield build(xs[:i] + [c] + xs[i + 1:])


def _int_candidates(v: int) -> Iterator[int]:
    """Simplest first: 0, then |v|, then magnitudes ascending (v >> k), then shrinking deltas toward v."""
    if v == 0:
        return
    yield 0
    sign = 1 if v > 0 else -1
    a = abs(v)
    if v < 0:
        yield a
    bits = a.bit_length()
    for k in range(bits - 1, 0, -1):          # 1, 2, 4, ... a/2 (scaled): coarse-to-fine magnitude search
        yield sign * (a >> k)
    for k in range(1, bits + 1):              # a - a/2, a - a/4, ..., a - 1: approach the failure boundary
        c = a - (a >> k)
        if 0 < c < a:
            yield sign * c
    if a > 1:
        yield sign * (a - 1)


def _float_candidates(v: float) -> Iterator[float]:
    if v == 0.0:
        return
    yield 0.0
    if not math.isfinite(v):
        yield 1.0
        return
    if v < 0:
        yield -v
    if v != int(v) and abs(v) < 2**53:
        yield float(int(v))
        for digits in range(0, 4):
            r = round(v, digits)
            if r != v:
                yield r
    for c in _int_candidates(int(v)) if abs(v) < 2**62 else ():
        yield float(c)
    yield v / 2


def shrink_case(case: dict[str, Any], predicate: Callable[[dict[str, Any]], bool],
                max_evaluations: int = 4000, time_budget_ms: float = 250.0,
                frozen: frozenset[str] = frozenset()) -> ShrinkResult:
    return Shrinker(predicate, max_evaluations, time_budget_ms, frozen).shrink(case)
