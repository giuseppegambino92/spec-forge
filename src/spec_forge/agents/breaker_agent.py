"""Breaker agent: adversarial, coverage-guided property falsification + minimal counterexamples.

Input generation is type-directed (annotations), precondition-directed (interval constraints
solved from ``:requires:``) and boundary-biased (0, +-1, interval endpoints, NaN/Inf/-0.0,
Unicode traps, empty/singleton/huge/all-equal/sorted collections). A line-arc coverage
tracer (``sys.settrace`` restricted to target frames) keeps a corpus of inputs that reached
new arcs; a share of trials mutate corpus members instead of sampling fresh values.

Concurrency targets are attacked with a thread harness under an adversarial scheduler
(switch interval 1 us) and compared with sequential executions.

``run_campaign`` executes *inside* the sandbox worker; ``BreakerAgent`` is the parent-side
facade that submits campaign jobs to the sandbox.
"""

from __future__ import annotations

import math
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..codec import case_complexity
from ..config import FuzzConfig, ShrinkConfig
from ..symbolic.ast_analyzer import TypeSpec
from ..symbolic.constraints import ConstraintSet, VarConstraint
from ..symbolic.invariants import CheckContext, Property, check_case
from ..symbolic.shrinker import shrink_case

TARGET_FILENAME = "<specforge-target>"

UNICODE_TRAPS = ["é", "é", "​", "\U0001d518", "\x00", "ß", "İ", "﻿", "‮", "\t", "\n", "\r\n",
                 "😀", "Ω", "ﬁ"]
ALPHABETS = ["ab", "a ", "  a", "abc", "aab ", "xyz019", "  \t", "ab-_.,", " "]


class InputGenerator:
    def __init__(self, params: list[dict[str, Any]], constraints: ConstraintSet, rng: random.Random,
                 max_len: int = 40) -> None:
        self.params = params
        self.cs = constraints
        self.rng = rng
        self.max_len = max_len

    # ------------------------------------------------------------------ helpers
    def _vc(self, key: str) -> VarConstraint:
        return self.cs.vars.get(key) or VarConstraint()

    @staticmethod
    def _int_bounds(vc: VarConstraint, default: tuple[int, int]) -> tuple[int, int]:
        lo = default[0] if vc.lo is None else (math.floor(vc.lo) + 1 if vc.lo_strict and float(vc.lo).is_integer()
                                               else math.ceil(vc.lo))
        hi = default[1] if vc.hi is None else (math.ceil(vc.hi) - 1 if vc.hi_strict and float(vc.hi).is_integer()
                                               else math.floor(vc.hi))
        if lo > hi:
            lo, hi = hi, lo
        return int(lo), int(hi)

    # ------------------------------------------------------------------ scalars
    def gen_int(self, key: str, size: int) -> int:
        vc = self._vc(key)
        bounded = vc.lo is not None or vc.hi is not None
        lo, hi = self._int_bounds(vc, (-(2**63), 2**63 - 1) if not bounded else (-10**6, 10**6))
        if vc.lo is not None and vc.hi is None:
            hi = max(lo, lo + 10**6)
        if vc.hi is not None and vc.lo is None:
            lo = min(hi, hi - 10**6)
        r = self.rng.random()
        if r < 0.3:
            cands = [v for v in (lo, lo + 1, hi, hi - 1, 0, 1, -1, 2, (lo + hi) // 2) if lo <= v <= hi]
            return self.rng.choice(cands)
        if r < 0.8:
            span = max(2, 2 ** min(62, 2 + size))
            a, b = max(lo, -span), min(hi, span)
            if a > b:
                a, b = lo, min(hi, lo + span)
            return self.rng.randint(a, b)
        if not bounded and r < 0.9:
            return self.rng.choice([1, -1]) * (2 ** self.rng.randint(8, 64) - self.rng.randint(0, 1))
        return self.rng.randint(lo, hi)

    def gen_float(self, key: str, size: int) -> float:
        vc = self._vc(key)
        lo = -1e6 if vc.lo is None else float(vc.lo)
        hi = 1e6 if vc.hi is None else float(vc.hi)
        specials = [0.0, -0.0, 0.1, 0.2, 0.3, 0.5, 1.0, -1.0, 1e-9, 2.5, 100.0, lo, hi]
        if vc.lo is None and vc.hi is None and not vc.finite:
            specials += [math.nan, math.inf, -math.inf, 1e308, -1e308, 5e-324]
        specials = [s for s in specials if (not math.isfinite(s)) or (lo <= s <= hi)]
        r = self.rng.random()
        if r < 0.3 and specials:
            v = self.rng.choice(specials)
        elif r < 0.6:
            v = round(self.rng.uniform(max(lo, -10.0 * size), min(hi, 10.0 * size)), 2)   # money-like
        elif r < 0.75:
            v = float(self.rng.randint(int(max(lo, -1000)), int(min(hi, 1000))))
        else:
            v = self.rng.uniform(lo, hi)
        if vc.lo_strict and v == lo:
            v = math.nextafter(lo, math.inf)
        if vc.hi_strict and v == hi:
            v = math.nextafter(hi, -math.inf)
        return v

    def gen_str(self, key: str, size: int) -> str:
        n = self.gen_len(key, size)
        r = self.rng.random()
        if r < 0.65:
            alphabet = self.rng.choice(ALPHABETS)
            return "".join(self.rng.choice(alphabet) for _ in range(n))
        if r < 0.85:
            pool = "ab " + "".join(UNICODE_TRAPS)
            chars = [self.rng.choice(UNICODE_TRAPS) if self.rng.random() < 0.3 else self.rng.choice("ab ")
                     for _ in range(n)]
            return "".join(chars) if pool else ""
        return "".join(chr(self.rng.randint(32, 126)) for _ in range(n))

    def gen_len(self, key: str, size: int) -> int:
        lo, hi = self._int_bounds(self._vc(f"len({key})"), (0, self.max_len))
        hi = min(hi, self.max_len) if hi >= lo else hi
        lo = max(0, lo)
        hi = max(lo, hi)
        r = self.rng.random()
        if r < 0.2:
            return self.rng.choice([v for v in (lo, lo + 1, lo + 2, hi) if lo <= v <= hi])
        return self.rng.randint(lo, max(lo, min(hi, size)))

    # ------------------------------------------------------------------ structured
    def gen(self, t: TypeSpec, key: str, size: int, depth: int = 0) -> Any:
        k = t.kind
        if k == "int":
            return self.gen_int(key, size)
        if k == "float":
            return self.gen_float(key, size)
        if k == "bool":
            return self.rng.random() < 0.5
        if k == "str":
            return self.gen_str(key, size)
        if k == "bytes":
            return bytes(self.rng.randint(0, 255) for _ in range(self.gen_len(key, size)))
        if k == "none":
            return None
        if k == "optional":
            if self.rng.random() < 0.2:
                return None
            return self.gen(t.args[0] if t.args else TypeSpec("int"), key, size, depth)
        if depth > 3:
            return [] if k == "list" else 0
        elem_key = f"{key}[*]"
        if k == "list":
            et = t.args[0] if t.args else TypeSpec("int")
            n = self.gen_len(key, size)
            shape = self.rng.random()
            if shape < 0.15 and n > 0:
                v = self.gen(et, elem_key, size, depth + 1)
                xs = [v for _ in range(n)]                      # all-equal (degenerate) collection
            elif shape < 0.3 and n > 0:
                pool = [self.gen(et, elem_key, size, depth + 1) for _ in range(max(1, n // 4))]
                xs = [self.rng.choice(pool) for _ in range(n)]  # heavy duplicates
            else:
                xs = [self.gen(et, elem_key, size, depth + 1) for _ in range(n)]
            if self._vc(key).sorted or (shape > 0.85 and _sortable(xs)):
                try:
                    xs = sorted(xs)
                except TypeError:
                    pass
            return xs
        if k == "tuple":
            if t.variadic:
                et = t.args[0] if t.args else TypeSpec("int")
                return tuple(self.gen(et, elem_key, size, depth + 1) for _ in range(self.gen_len(key, size)))
            return tuple(self.gen(a, f"{key}[{i}]", size, depth + 1) for i, a in enumerate(t.args))
        if k == "set":
            et = t.args[0] if t.args else TypeSpec("int")
            out = set()
            for _ in range(self.gen_len(key, size)):
                try:
                    out.add(self.gen(et, elem_key, size, depth + 1))
                except TypeError:
                    pass
            return out
        if k == "dict":
            kt = t.args[0] if t.args else TypeSpec("str")
            vt = t.args[1] if len(t.args) > 1 else TypeSpec("int")
            out_d: dict[Any, Any] = {}
            for _ in range(self.gen_len(key, size)):
                try:
                    out_d[self.gen(kt, f"{key}.key", min(size, 6), depth + 1)] = self.gen(vt, elem_key, size,
                                                                                          depth + 1)
                except TypeError:
                    pass
            return out_d
        return self.gen_int(key, size)

    def gen_case(self, size: int, params: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        case: dict[str, Any] = {}
        for p in (params if params is not None else self.params):
            name = p["name"]
            constrained = any(k == name or k.startswith(f"{name}[") or k == f"len({name})" for k in self.cs.vars)
            if p["has_default"] and not constrained and self.rng.random() < 0.4:
                continue  # exercise the default value
            case[name] = self.gen(TypeSpec.from_dict(p["type"]), name, size)  # type: ignore[arg-type]
        return case

    # ------------------------------------------------------------------ mutation (coverage-guided)
    def mutate(self, case: dict[str, Any], size: int) -> dict[str, Any]:
        if not case:
            return self.gen_case(size)
        out = dict(case)
        key = self.rng.choice(sorted(out))
        out[key] = self._mutate_value(out[key], key, size)
        return out

    def _mutate_value(self, v: Any, key: str, size: int) -> Any:
        r = self.rng.random()
        if isinstance(v, bool):
            return not v
        if isinstance(v, int):
            return self.rng.choice([v + 1, v - 1, v * 2, -v, 0, v // 2, self.gen_int(key, size)])
        if isinstance(v, float):
            return self.rng.choice([v + 0.1, v * -1, v / 3 if v else 0.1, round(v, 1), self.gen_float(key, size)])
        if isinstance(v, str):
            if not v or r < 0.4:
                i = self.rng.randint(0, len(v))
                return v[:i] + self.rng.choice([" ", "  ", "a", self.rng.choice(UNICODE_TRAPS)]) + v[i:]
            if r < 0.7:
                i = self.rng.randrange(len(v))
                return v[:i] + v[i + 1:]
            return v + v
        if isinstance(v, list):
            xs = list(v)
            if not xs or r < 0.35:
                xs.insert(self.rng.randint(0, len(xs)), xs[0] if xs and self.rng.random() < 0.5 else
                          (self._mutate_value(xs[-1], f"{key}[*]", size) if xs else 0))
            elif r < 0.6:
                del xs[self.rng.randrange(len(xs))]
            elif r < 0.8:
                i = self.rng.randrange(len(xs))
                xs[i] = self._mutate_value(xs[i], f"{key}[*]", size)
            else:
                xs = xs + xs[-1:]
            if self._vc(key).sorted and _sortable(xs):
                xs = sorted(xs)
            return xs
        return v


def _sortable(xs: list[Any]) -> bool:
    try:
        sorted(xs)
        return True
    except TypeError:
        return False


class CoverageTracker:
    """Line-arc coverage for frames compiled from the target module only."""

    def __init__(self) -> None:
        self.arcs: set[tuple[int, int]] = set()
        self._new = 0

    def run(self, fn: Any) -> tuple[Any, int]:
        before = len(self.arcs)
        prev = {"line": 0}

        def local(frame: Any, event: str, arg: Any) -> Any:
            if event == "line":
                self.arcs.add((prev["line"], frame.f_lineno))
                prev["line"] = frame.f_lineno
            return local

        def glob(frame: Any, event: str, arg: Any) -> Any:
            if frame.f_code.co_filename == TARGET_FILENAME:
                return local
            return None

        old = sys.gettrace()
        sys.settrace(glob)
        try:
            result = fn()
        finally:
            sys.settrace(old)
        return result, len(self.arcs) - before


# --------------------------------------------------------------------------- campaign (worker side)


@dataclass
class CampaignResult:
    trials: int = 0
    rejected: int = 0
    elapsed_ms: float = 0.0
    first_failure_trial: Optional[int] = None
    first_failure_ms: Optional[float] = None
    counterexamples: list[dict[str, Any]] = field(default_factory=list)
    failing_cases: list[dict[str, Any]] = field(default_factory=list)
    passing: list[dict[str, Any]] = field(default_factory=list)
    coverage_arcs: int = 0
    property_failures: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _class_case(gen: InputGenerator, spec_params: list[dict[str, Any]], ctor: list[dict[str, Any]], size: int,
                fuzz: FuzzConfig) -> dict[str, Any]:
    tlo, thi = fuzz.concurrency_threads
    clo, chi = fuzz.concurrency_calls
    return {
        "threads": gen.rng.randint(tlo, thi),
        "calls": gen.rng.randint(clo, chi),
        "args": gen.gen_case(min(size, 8), spec_params),
        "init": gen.gen_case(min(size, 8), [p for p in ctor if not p["has_default"]]),
    }


def make_predicate(ctx: CheckContext, props: list[Property], prop_name: str, retries: int = 1):
    def still_fails(trial: dict[str, Any]) -> bool:
        if ctx.spec.kind == "function" and not ctx.precondition_holds(trial):
            return False
        for _ in range(max(1, retries)):
            if any(n == prop_name for n, _ in check_case(ctx, props, trial, only=prop_name)):
                return True
        return False

    return still_fails


def run_campaign(ctx: CheckContext, props: list[Property], fuzz: FuzzConfig, shrink_cfg: ShrinkConfig,
                 trials: Optional[int] = None, seed: Optional[int] = None, shrink: bool = True,
                 stop_early: bool = True) -> CampaignResult:
    rng = random.Random(fuzz.seed if seed is None else seed)
    spec = ctx.spec
    gen = InputGenerator(spec.params, ConstraintSet.from_dict(spec.constraints), rng, fuzz.max_collection_len)
    tracker = CoverageTracker() if (fuzz.coverage_guided and spec.kind == "function") else None
    corpus: list[dict[str, Any]] = []
    res = CampaignResult()
    total = trials if trials is not None else fuzz.trials
    t0 = time.perf_counter()
    stop_at: Optional[int] = None
    seen_props: set[str] = set()
    is_class = spec.kind == "class"

    for i in range(total):
        if stop_at is not None and i >= stop_at:
            break
        size = fuzz.max_collection_len if rng.random() < 0.2 else min(fuzz.max_collection_len, 2 + i // 3)
        size = max(size, fuzz.min_size)
        case: Optional[dict[str, Any]] = None
        for _ in range(fuzz.precondition_retries):
            if is_class:
                cand = _class_case(gen, spec.params, spec.ctor_params, size, fuzz)
            elif corpus and tracker is not None and i % 4 == 3:
                cand = gen.mutate(rng.choice(corpus), size)
            else:
                cand = gen.gen_case(size)
            if is_class or ctx.precondition_holds(cand):
                case = cand
                break
            res.rejected += 1
        if case is None:
            continue
        res.trials += 1
        if tracker is not None:
            failures, new_arcs = tracker.run(lambda: check_case(ctx, props, case))  # noqa: B023
            if new_arcs and len(corpus) < 64:
                corpus.append(case)
        else:
            failures = check_case(ctx, props, case)
        if not failures:
            if len(res.passing) < fuzz.collect_passing:
                res.passing.append(case)
            continue
        for name, _ in failures:
            res.property_failures[name] = res.property_failures.get(name, 0) + 1
        if res.first_failure_trial is None:
            res.first_failure_trial = res.trials
            res.first_failure_ms = (time.perf_counter() - t0) * 1000
            stop_at = i + 1 + (fuzz.extra_trials_after_failure if stop_early else total)
        if len(res.failing_cases) < 3 * fuzz.max_counterexamples:
            res.failing_cases.append(case)
        name, msg = failures[0]
        new = [f for f in failures if f[0] not in seen_props]
        if new and len(res.counterexamples) < fuzz.max_counterexamples:
            name, msg = new[0]
            seen_props.add(name)
            cex: dict[str, Any] = {"property": name, "message": msg, "initial_case": case, "trial": res.trials,
                                   "minimal_case": case, "minimal_message": msg,
                                   "initial_complexity": case_complexity(case),
                                   "final_complexity": case_complexity(case)}
            if shrink:
                pred = make_predicate(ctx, props, name, shrink_cfg.flaky_retries if is_class else 1)
                sr = shrink_case(case, pred, shrink_cfg.max_evaluations, shrink_cfg.time_budget_ms)
                cex.update(minimal_case=sr.minimal, initial_complexity=sr.initial_complexity,
                           final_complexity=sr.final_complexity, shrink_ms=round(sr.elapsed_ms, 3),
                           shrink_evaluations=sr.evaluations, shrink_reduction=round(sr.reduction, 4),
                           shrink_budget_exhausted=sr.budget_exhausted)
                again = [m for n, m in check_case(ctx, props, sr.minimal, only=name)]
                if again:
                    cex["minimal_message"] = again[0]
            res.counterexamples.append(cex)
    res.elapsed_ms = (time.perf_counter() - t0) * 1000
    res.coverage_arcs = len(tracker.arcs) if tracker else 0
    return res


# --------------------------------------------------------------------------- parent-side facade


class BreakerAgent:
    """Submits falsification campaigns for a spec to the sandbox and returns counterexamples."""

    def __init__(self, runner: Any, fuzz: FuzzConfig, shrink: ShrinkConfig) -> None:
        self.runner = runner
        self.fuzz = fuzz
        self.shrink = shrink

    def attack(self, source: str, spec: Any, seed: Optional[int] = None, trials: Optional[int] = None,
               shrink: bool = True, stop_early: bool = True) -> dict[str, Any]:
        return self.runner.submit({
            "op": "campaign", "source": source, "spec": spec.to_dict(),
            "fuzz": self.fuzz.__dict__, "shrink_cfg": self.shrink.__dict__,
            "seed": self.fuzz.seed if seed is None else seed, "trials": trials, "shrink": shrink,
            "stop_early": stop_early,
        })
