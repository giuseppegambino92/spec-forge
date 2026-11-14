"""Formal property DSL and its executable semantics.

A *spec* is pure data (``TargetSpec`` + ``PropertySpec``) so it can be shipped to the
sandbox worker as JSON. Inside the worker, ``build_properties`` compiles the data into
executable checkers that run against a loaded module namespace.

Property kinds
--------------
Totality            under the precondition the call terminates and raises only documented exceptions
Postcondition       ``:ensures:`` expression over inputs and ``result`` holds
Law                 ``:law:`` metamorphic relation (may call module functions) holds
Idempotency         f(f(x)) == f(x)
RoundTrip           decode(encode(x)) == x
PermutationInvariance  aggregate(xs) == aggregate(permutation(xs))
NoInputMutation     arguments are not mutated (functional purity of the interface)
Determinism         equal inputs give equal outputs across calls (no hidden state)
FiniteOutput        finite inputs never yield NaN/Inf
Atomicity           concurrent calls are equivalent to a sequential execution (no lost updates)
"""

from __future__ import annotations

import copy
import math
import signal
import statistics
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Optional

from ..codec import safe_repr, sem_eq

# --------------------------------------------------------------------------- data model


@dataclass
class PropertySpec:
    kind: str
    name: str
    description: str
    origin: str = "inferred"           # inferred | docstring
    confidence: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PropertySpec":
        return cls(**d)


@dataclass
class TargetSpec:
    name: str
    kind: str                                  # function | class
    params: list[dict[str, Any]]               # ParamFact dicts (method params for class targets)
    requires: list[str] = field(default_factory=list)
    allowed_raises: list[str] = field(default_factory=list)
    properties: list[PropertySpec] = field(default_factory=list)
    constraints: dict[str, dict[str, Any]] = field(default_factory=dict)
    lineno: int = 0
    end_lineno: int = 0
    method: Optional[str] = None               # class targets: method under concurrency test
    ctor_params: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["properties"] = [p.to_dict() for p in self.properties]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TargetSpec":
        d = dict(d)
        d["properties"] = [PropertySpec.from_dict(p) for p in d.get("properties", [])]
        return cls(**d)


# --------------------------------------------------------------------------- guarded calls


TAINTED = {"stray_threads": 0}  # threads still alive after a check -> worker should be recycled


class CallTimeout(BaseException):
    """Raised inside a target that exceeds its wall-clock budget (BaseException: not swallowable
    by ``except Exception`` in target code)."""


_timer_depth = 0


def _on_alarm(signum: int, frame: Any) -> None:  # pragma: no cover - signal path
    raise CallTimeout()


def guarded_call(fn: Callable[[], Any], timeout_s: float) -> Any:
    """Run ``fn`` with a wall-clock limit (SIGALRM timer on the main thread, nestable)."""
    global _timer_depth
    use_timer = (timeout_s > 0 and _timer_depth == 0 and hasattr(signal, "setitimer")
                 and threading.current_thread() is threading.main_thread())
    if not use_timer:
        return fn()
    old = signal.signal(signal.SIGALRM, _on_alarm)
    _timer_depth += 1
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        _timer_depth -= 1
        signal.signal(signal.SIGALRM, old)


def invoke(fn: Callable[..., Any], params: list[dict[str, Any]], case: dict[str, Any]) -> Any:
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    positional_ok = True
    for p in params:
        name = p["name"]
        if name not in case:
            positional_ok = False
            continue
        v = case[name]
        if p["kind"] == "varargs":
            args.extend(v)
        elif p["kind"] == "varkw":
            kwargs.update(v)
        elif p["kind"] == "kwonly" or not positional_ok:
            kwargs[name] = v
        else:
            args.append(v)
    return fn(*args, **kwargs)


# --------------------------------------------------------------------------- contract eval

SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "frozenset", "int",
    "isinstance", "len", "list", "map", "max", "min", "pow", "range", "repr", "reversed", "round", "set",
    "sorted", "str", "sum", "tuple", "zip", "bytes", "chr", "ord", "hash", "iter", "next", "slice",
    "True", "False", "None", "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
    "ArithmeticError", "OverflowError", "Exception",
)


def contract_namespace(module_ns: dict[str, Any]) -> dict[str, Any]:
    import builtins as _b

    safe = {n: getattr(_b, n) for n in SAFE_BUILTIN_NAMES if hasattr(_b, n)}
    ns: dict[str, Any] = {k: v for k, v in module_ns.items() if not k.startswith("__")}
    ns.update({"math": math, "statistics": statistics, "Decimal": Decimal, "Fraction": Fraction,
               "isclose": math.isclose, "__builtins__": safe})
    return ns


@dataclass
class Observation:
    result: Any = None
    exc: Optional[BaseException] = None
    inputs_after: dict[str, Any] = field(default_factory=dict)

    @property
    def exc_name(self) -> str:
        return type(self.exc).__name__ if self.exc is not None else ""


class CheckContext:
    """Everything a property needs to evaluate one target inside a loaded module."""

    def __init__(self, module_ns: dict[str, Any], spec: TargetSpec, timeout_s: float = 0.5) -> None:
        self.ns = module_ns
        self.spec = spec
        self.timeout_s = timeout_s
        self.cns = contract_namespace(module_ns)
        self.params = spec.params
        self.target: Any = module_ns.get(spec.name)
        self._compiled: dict[str, Any] = {}
        self._requires = [self.compile(e) for e in spec.requires]

    def compile(self, expr: str) -> Any:
        code = self._compiled.get(expr)
        if code is None:
            code = compile(expr, "<contract>", "eval")
            self._compiled[expr] = code
        return code

    def eval_expr(self, expr: str, bindings: dict[str, Any]) -> Any:
        scope = dict(self.cns)
        scope.update(bindings)
        return eval(self.compile(expr), scope)  # noqa: S307 - firewall-verified contract expression

    def precondition_holds(self, case: dict[str, Any]) -> bool:
        if not self._requires:
            return True
        scope = dict(self.cns)
        scope.update(copy.deepcopy(case))
        try:
            return all(bool(eval(c, scope)) for c in self._requires)  # noqa: S307
        except Exception:
            return False

    def call(self, case: dict[str, Any], fn: Any = None) -> Observation:
        fn = fn if fn is not None else self.target
        passed = copy.deepcopy(case)
        obs = Observation(inputs_after=passed)
        try:
            obs.result = guarded_call(lambda: invoke(fn, self.params, passed), self.timeout_s)
        except CallTimeout as exc:
            obs.exc = TimeoutError(f"did not terminate within {self.timeout_s}s")
            obs.exc.__cause__ = exc
        except RecursionError as exc:
            obs.exc = exc
        except Exception as exc:  # noqa: BLE001 - any target exception is data
            obs.exc = exc
        return obs

    def raw_call(self, case: dict[str, Any], fn: Any = None) -> Any:
        fn = fn if fn is not None else self.target
        return guarded_call(lambda: invoke(fn, self.params, copy.deepcopy(case)), self.timeout_s)


def _snapshot(v: Any) -> Any:
    try:
        return copy.deepcopy(v)
    except Exception:
        return v


def _all_finite(v: Any) -> bool:
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, (list, tuple, set, frozenset)):
        return all(_all_finite(x) for x in v)
    if isinstance(v, dict):
        return all(_all_finite(x) for x in v.values())
    return True


# --------------------------------------------------------------------------- properties


class Property:
    kind = "abstract"
    needs_observation = True

    def __init__(self, spec: PropertySpec, ctx: CheckContext) -> None:
        self.spec = spec
        self.ctx = ctx
        self.name = spec.name

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError


class Totality(Property):
    kind = "totality"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        if obs.exc is None:
            return None
        allowed = set(self.ctx.spec.allowed_raises)
        if obs.exc_name in allowed and not isinstance(obs.exc, TimeoutError):
            return None
        return f"raised {obs.exc_name}: {safe_repr(str(obs.exc), 120)}"


class Postcondition(Property):
    kind = "postcondition"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        if obs.exc is not None:
            return None
        expr = self.spec.params["expr"]
        bindings = dict(case)
        bindings.update({f"old_{k}": v for k, v in case.items()})
        bindings["result"] = obs.result
        try:
            ok = guarded_call(lambda: self.ctx.eval_expr(expr, bindings), self.ctx.timeout_s)
        except CallTimeout:
            return f"ensures `{expr}` did not terminate"
        except Exception as exc:  # noqa: BLE001
            return f"ensures `{expr}` raised {type(exc).__name__}: {exc} (result={safe_repr(obs.result, 80)})"
        if not ok:
            return f"ensures `{expr}` violated (result={safe_repr(obs.result, 100)})"
        return None


class Law(Property):
    kind = "law"
    needs_observation = False

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        expr = self.spec.params["expr"]
        try:
            ok = guarded_call(lambda: self.ctx.eval_expr(expr, copy.deepcopy(case)), self.ctx.timeout_s * 4)
        except CallTimeout:
            return f"law `{expr}` did not terminate"
        except Exception as exc:  # noqa: BLE001
            return f"law `{expr}` raised {type(exc).__name__}: {safe_repr(str(exc), 80)}"
        return None if ok else f"law `{expr}` violated"


class Idempotency(Property):
    kind = "idempotency"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        if obs.exc is not None or not self.ctx.params:
            return None
        first = self.ctx.params[0]["name"]
        y = _snapshot(obs.result)
        case2 = dict(case)
        case2[first] = y
        if not self.ctx.precondition_holds(case2):
            return None
        o2 = self.ctx.call(case2)
        if o2.exc is not None:
            return f"f(f(x)) raised {o2.exc_name}: {o2.exc}"
        if not sem_eq(o2.result, y):
            return f"not idempotent: f(x)={safe_repr(y, 70)} but f(f(x))={safe_repr(o2.result, 70)}"
        return None


class RoundTrip(Property):
    kind = "roundtrip"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        if obs.exc is not None:
            return None
        decoder = self.ctx.ns.get(self.spec.params["decoder"])
        param = self.spec.params["param"]
        if decoder is None or param not in case:
            return None
        try:
            back = guarded_call(lambda: decoder(_snapshot(obs.result)), self.ctx.timeout_s)
        except CallTimeout:
            return "decode(encode(x)) did not terminate"
        except Exception as exc:  # noqa: BLE001
            return f"decode(encode(x)) raised {type(exc).__name__}: {exc}"
        if not sem_eq(back, case[param]):
            return (f"round-trip broken: {self.spec.params['decoder']}({safe_repr(obs.result, 60)}) = "
                    f"{safe_repr(back, 60)} != {safe_repr(case[param], 60)}")
        return None


class PermutationInvariance(Property):
    kind = "permutation_invariance"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        if obs.exc is not None:
            return None
        param = self.spec.params["param"]
        xs = case.get(param)
        if not isinstance(xs, list) or len(xs) < 2:
            return None
        for label, perm in (("reversed", list(reversed(xs))), ("rotated", xs[1:] + xs[:1])):
            c2 = dict(case)
            c2[param] = perm
            o2 = self.ctx.call(c2)
            if o2.exc is None and not sem_eq(o2.result, obs.result):
                return (f"order-dependent result: f(xs)={safe_repr(obs.result, 50)} but "
                        f"f({label}(xs))={safe_repr(o2.result, 50)}")
        return None


class NoInputMutation(Property):
    kind = "no_input_mutation"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        for p in self.spec.params.get("params", []):
            if p in case and not sem_eq(obs.inputs_after.get(p), case[p]):
                return (f"argument `{p}` mutated: {safe_repr(case[p], 60)} -> "
                        f"{safe_repr(obs.inputs_after.get(p), 60)}")
        return None


class Determinism(Property):
    kind = "determinism"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        if obs.exc is not None:
            return None
        first = _snapshot(obs.result)
        o2 = self.ctx.call(case)
        if o2.exc is None and not sem_eq(first, o2.result):
            return (f"hidden state: identical calls returned {safe_repr(first, 60)} then "
                    f"{safe_repr(o2.result, 60)}")
        return None


class FiniteOutput(Property):
    kind = "finite_output"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        if obs.exc is not None or not _all_finite(case):
            return None
        if not _all_finite(obs.result):
            return f"non-finite output {safe_repr(obs.result, 80)} from finite inputs"
        return None


class Atomicity(Property):
    """Concurrent execution must match the sequential execution of the same call multiset."""

    kind = "atomicity"

    def check(self, case: dict[str, Any], obs: Observation) -> Optional[str]:
        cls = self.ctx.ns.get(self.ctx.spec.name)
        method = self.spec.params["method"]
        attrs: list[str] = self.spec.params["attrs"]
        threads, calls = int(case["threads"]), int(case["calls"])
        args = case.get("args", {})
        init = case.get("init", {})
        if cls is None or threads < 1 or calls < 1:
            return None
        params = [p for p in self.ctx.spec.params]
        try:
            shared = cls(**copy.deepcopy(init))
            seq = cls(**copy.deepcopy(init))
        except Exception as exc:  # noqa: BLE001
            return f"constructor raised {type(exc).__name__}: {exc}"
        errors: list[str] = []
        barrier = threading.Barrier(threads)

        def worker() -> None:
            try:
                barrier.wait(timeout=2)
                fn = getattr(shared, method)
                for _ in range(calls):
                    invoke(fn, params, copy.deepcopy(args))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        budget = max(1.0, self.ctx.timeout_s * 6)
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # adversarial scheduler: maximise preemption points
        try:
            ts = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
            for t in ts:
                t.start()
            deadline = time.monotonic() + budget
            for t in ts:
                t.join(timeout=max(0.0, deadline - time.monotonic()))
        finally:
            sys.setswitchinterval(old)
        alive = sum(1 for t in ts if t.is_alive())
        if alive:
            TAINTED["stray_threads"] += alive
            return f"deadlock/livelock: {alive} worker thread(s) still running after {budget:.1f}s"
        if errors:
            return f"concurrent call raised {errors[0]}"
        fn_seq = getattr(seq, method)

        def sequential() -> None:
            for _ in range(threads * calls):
                invoke(fn_seq, params, copy.deepcopy(args))

        try:
            guarded_call(sequential, budget)
        except CallTimeout:
            return f"sequential execution of {threads * calls} calls exceeded {budget:.1f}s"
        except Exception as exc:  # noqa: BLE001
            return f"sequential call raised {type(exc).__name__}: {exc}"
        for a in attrs:
            got, want = getattr(shared, a, None), getattr(seq, a, None)
            if not sem_eq(got, want):
                return (f"lost update on `{a}`: {threads} threads x {calls} calls -> {safe_repr(got, 40)}, "
                        f"sequential execution -> {safe_repr(want, 40)}")
        return None


PROPERTY_TYPES: dict[str, type[Property]] = {
    cls.kind: cls for cls in (Totality, Postcondition, Law, Idempotency, RoundTrip, PermutationInvariance,
                              NoInputMutation, Determinism, FiniteOutput, Atomicity)
}


def build_properties(ctx: CheckContext) -> list[Property]:
    return [PROPERTY_TYPES[p.kind](p, ctx) for p in ctx.spec.properties if p.kind in PROPERTY_TYPES]


def check_case(ctx: CheckContext, props: list[Property], case: dict[str, Any],
               only: Optional[str] = None) -> list[tuple[str, str]]:
    """Evaluate ``case`` against all (or one named) properties. Returns [(property name, message)]."""
    selected = [p for p in props if only is None or p.name == only]
    if not selected:
        return []
    if ctx.spec.kind == "class":
        obs = Observation()
    elif any(p.needs_observation for p in selected):
        obs = ctx.call(case)
    else:
        obs = Observation()
    failures: list[tuple[str, str]] = []
    for p in selected:
        try:
            msg = p.check(case, obs)
        except CallTimeout:
            msg = "property evaluation did not terminate"
        except Exception as exc:  # noqa: BLE001 - checker crash is reported, never fatal
            msg = f"property check crashed: {type(exc).__name__}: {exc}"
        if msg:
            failures.append((p.name, msg))
    return failures
