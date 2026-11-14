"""Symbolic core: contract parsing, type/constraint inference, property semantics, delta-debugging."""

from __future__ import annotations

import math
import time

import pytest

from spec_forge.agents.spec_agent import SpecAgent
from spec_forge.codec import case_complexity, complexity, dumps, loads, sem_eq
from spec_forge.config import SandboxPolicy
from spec_forge.sandbox.runner import load_module
from spec_forge.symbolic.ast_analyzer import analyze_module, parse_contract
from spec_forge.symbolic.constraints import solve_preconditions
from spec_forge.symbolic.invariants import CheckContext, build_properties, check_case, guarded_call, CallTimeout
from spec_forge.symbolic.shrinker import shrink_case


def ctx_for(src: str, name: str):
    spec = SpecAgent().extract(src).target(name)
    assert spec is not None
    ctx = CheckContext(load_module(src, SandboxPolicy()), spec)
    return ctx, build_properties(ctx)


def failing(ctx, props, case):
    return {n for n, _ in check_case(ctx, props, case)}


# ----------------------------------------------------------------------------- parsing & inference

def test_contract_parser_preserves_literals_and_joins_continuations():
    doc = '''Summary.

    :requires: 0 <= n <= 10
    :ensures: "  " not in result
    :law: all(f(x) for x in [1,
        2, 3])
    :raises: ValueError, KeyError
    :idempotent:
    '''
    c = parse_contract(doc)
    assert c.requires == ["0 <= n <= 10"]
    assert c.ensures == ['"  " not in result']
    assert c.laws == ["all(f(x) for x in [1, 2, 3])"]
    assert c.raises == ["ValueError", "KeyError"]
    assert "idempotent" in c.flags


def test_unparseable_clause_is_reported_not_silently_used():
    c = parse_contract(":ensures: result >\n")
    assert c.ensures == [] and c.parse_errors


def test_interval_solver_extracts_bounds_lengths_elements_and_shapes():
    cs = solve_preconditions([
        "0 <= x <= 2**32 - 1", "n > 3", "1 <= len(xs) <= 40", "all(-5 <= v <= 5 for v in xs)",
        "ts == sorted(ts)", "all(math.isfinite(a) and abs(a) <= 1e9 for a in amounts)",
        "all(0 <= n <= 4096 for _, n in runs)",
    ]).vars
    assert (cs["x"].lo, cs["x"].hi) == (0, 2**32 - 1)
    assert cs["n"].lo == 3 and cs["n"].lo_strict
    assert (cs["len(xs)"].lo, cs["len(xs)"].hi) == (1, 40)
    assert (cs["xs[*]"].lo, cs["xs[*]"].hi) == (-5, 5)
    assert cs["ts"].sorted
    assert cs["amounts[*]"].finite and cs["amounts[*]"].hi == 1e9 and cs["amounts[*]"].lo == -1e9
    assert cs["runs[*][1]"].hi == 4096


def test_ast_analyzer_dataflow_facts():
    src = '''
import threading

def f(xs: list[int], acc: list = [], *, k: int = 2) -> int:
    xs.sort()
    acc.append(1)
    return xs[k] // len(acc) - 1

class Counter:
    """Shared between worker threads."""
    def __init__(self):
        self.v = 0
    def bump(self):
        cur = self.v
        self.v = cur + 1
'''
    m = analyze_module(src)
    f = m.function("f")
    assert set(f.mutated_params) == {"xs", "acc"}
    assert [p.default_is_mutable for p in f.params] == [False, True, False]
    assert {r.kind for r in f.risk_sites} >= {"division", "index", "subtraction"}
    c = m.klass("Counter")
    assert c.mentions_concurrency
    assert next(x for x in c.methods if x.name == "bump").rmw_attrs == ["v"]


def test_spec_agent_infers_property_families():
    src = '''
def normalize_tag(s: str) -> str:
    return s.strip().lower()

def to_wire(x: int) -> str:
    return str(x)

def from_wire(s: str) -> int:
    return int(s)

def basket_total(prices: list[float]) -> float:
    return sum(prices)

def first(xs: list[int], cache: dict = {}) -> int:
    return xs[0]
'''
    spec = SpecAgent().extract(src)
    kinds = {t.name: {p.kind for p in t.properties} for t in spec.targets}
    assert "idempotency" in kinds["normalize_tag"]
    assert "roundtrip" in kinds["to_wire"]
    assert {"permutation_invariance", "finite_output"} <= kinds["basket_total"]
    assert {"determinism", "no_input_mutation", "totality"} <= kinds["first"]


# ----------------------------------------------------------------------------- property semantics

def test_postcondition_and_totality_with_documented_raises():
    src = '''
def safe_div(a: int, b: int) -> float:
    """:ensures: result * b == a or abs(result * b - a) < 1e-6
    """
    if b == 0:
        raise ValueError("b must be non-zero")
    return a / b
'''
    ctx, props = ctx_for(src, "safe_div")
    assert failing(ctx, props, {"a": 3, "b": 0}) == set()           # documented ValueError
    assert failing(ctx, props, {"a": 3, "b": 4}) == set()


def test_idempotency_roundtrip_mutation_determinism_detected():
    src = '''
def squash(s: str) -> str:
    return s.replace("aa", "a")

def enc(x: str) -> str:
    return x[::-1] + "!"

def dec(x: str) -> str:
    return x[::-1]

def grab(xs: list[int], seen: list[int] = []) -> list[int]:
    seen.extend(xs)
    xs.clear()
    return list(seen)
'''
    spec = SpecAgent().extract(src)
    spec.target("enc").properties.append(
        __import__("spec_forge.symbolic.invariants", fromlist=["PropertySpec"]).PropertySpec(
            "roundtrip", "rt", "dec(enc(x)) == x", params={"decoder": "dec", "param": "x"}))
    ns = load_module(src, SandboxPolicy())
    for name, case, expected in (("squash", {"s": "aaaa"}, "idempotency"), ("enc", {"x": "ab"}, "rt"),
                                 ("grab", {"xs": [1]}, "no_input_mutation"), ("grab", {"xs": [2]}, "determinism")):
        ctx = CheckContext(ns, spec.target(name))
        names = {n for n, _ in check_case(ctx, build_properties(ctx), case)}
        assert expected in names, (name, names)


def test_permutation_invariance_catches_float_drift_and_fsum_fixes_it():
    bad = "def ledger_total(xs: list[float]) -> float:\n    return sum(xs)\n"
    good = "import math\ndef ledger_total(xs: list[float]) -> float:\n    return math.fsum(xs)\n"
    case = {"xs": [0.1, 0.2, 0.3]}
    ctx, props = ctx_for(bad, "ledger_total")
    assert "permutation_invariance" in failing(ctx, props, case)
    ctx, props = ctx_for(good, "ledger_total")
    assert failing(ctx, props, case) == set()


def test_law_can_call_module_functions():
    src = '''
def paginate(items: list[int], page: int, size: int) -> list[int]:
    """
    :requires: 1 <= size
    :law: [x for p in range(len(items) // size + 1) for x in paginate(items, p, size)] == items
    """
    return items[page * size:page * size + size - 1]
'''
    ctx, props = ctx_for(src, "paginate")
    assert "law[0]" in failing(ctx, props, {"items": [1, 2], "page": 0, "size": 2})


def test_guarded_call_interrupts_non_termination():
    t0 = time.perf_counter()
    with pytest.raises(CallTimeout):
        guarded_call(lambda: [None for _ in iter(int, 1)], 0.1)
    assert time.perf_counter() - t0 < 1.0


def test_totality_reports_timeouts_as_violations():
    src = "def spin(n: int) -> int:\n    while n != 0:\n        n += 1\n    return n\n"
    ctx, props = ctx_for(src, "spin")
    ctx.timeout_s = 0.05
    msgs = check_case(ctx, props, {"n": 1})
    assert msgs and "did not terminate" in msgs[0][1]


# ----------------------------------------------------------------------------- codec

def test_codec_roundtrip_and_nan_aware_equality():
    v = {"t": (1, 2.5), "s": {3, 1}, "f": [math.nan, math.inf, -0.0], "b": b"\x00\xff", "n": None}
    back = loads(dumps(v))
    assert back["t"] == (1, 2.5) and back["s"] == {1, 3} and back["b"] == b"\x00\xff"
    assert sem_eq(back["f"], [math.nan, math.inf, -0.0])
    assert not sem_eq([1], (1,))


def test_case_complexity_ignores_parameter_names():
    assert case_complexity({"a_very_long_parameter_name": 0}) == complexity(0)


# ----------------------------------------------------------------------------- shrinking

def test_shrinks_large_list_to_minimal_counterexample_fast():
    big = list(range(-25, 25)) + [{"k": "v" * 10}] * 0
    pred = lambda c: sum(x for x in c["xs"] if x > 0) > 20 and len(c["xs"]) >= 1  # noqa: E731
    res = shrink_case({"xs": big}, pred)
    assert pred(res.minimal)
    xs = res.minimal["xs"]
    # 1-minimal (ddmin guarantee): removing any single element makes the failure disappear
    assert all(not pred({"xs": xs[:i] + xs[i + 1:]}) for i in range(len(xs)))
    assert res.reduction > 0.8
    assert res.elapsed_ms < 50
    assert all(a >= b for a, b in zip(res.steps, res.steps[1:]))  # monotone progress


def test_shrinks_integers_to_failure_boundary():
    res = shrink_case({"remaining": 404116653, "cost": 1255293587}, lambda c: c["cost"] > c["remaining"])
    assert res.minimal == {"remaining": 0, "cost": 1}


def test_shrinks_strings_nested_structures_and_translates_sequences():
    res = shrink_case({"s": "xyz  abc   def"}, lambda c: "   " in c["s"])
    assert res.minimal["s"] == "   "
    res = shrink_case({"d": {"a": [5, 6, 7], "b": "zz"}}, lambda c: any(len(v) >= 2 for v in c["d"].values()))
    assert case_complexity(res.minimal) <= case_complexity({"d": {"b": "  "}})
    res = shrink_case({"ts": [66, 72]}, lambda c: len(c["ts"]) == 2 and c["ts"][1] - c["ts"][0] == 6)
    assert res.minimal["ts"] == [0, 6]


def test_shrinker_respects_budget():
    res = shrink_case({"xs": list(range(2000))}, lambda c: len(c["xs"]) > 1500 and sum(c["xs"]) % 7 == 3,
                      max_evaluations=5)
    assert res.evaluations <= 5 and res.budget_exhausted
