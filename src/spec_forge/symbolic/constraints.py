"""Symbolic precondition solving over an interval abstract domain.

``:requires:`` clauses are parsed (never executed) and linear comparison chains are
propagated into per-variable intervals so the Breaker can generate inputs *inside* the
precondition and concentrate on its boundaries instead of relying on rejection sampling.

Recognised shapes (any combination, conjunctions via ``and``):
    a <= x <= b,  x > c,  c >= x,  len(xs) >= n,  n <= len(xs) <= m
    all(lo <= v <= hi for v in xs)       -> element interval for xs
    all(math.isfinite(v) for v in xs)    -> finite elements
    math.isfinite(x)                     -> finite scalar
    xs == sorted(xs)                     -> sorted list

Clauses that do not fit the domain are still enforced at runtime by the precondition
filter; the solver only narrows the search space (sound over-approximation).
"""

from __future__ import annotations

import ast
import math
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class VarConstraint:
    lo: Optional[float] = None
    hi: Optional[float] = None
    lo_strict: bool = False
    hi_strict: bool = False
    finite: bool = False
    sorted: bool = False

    def tighten_lo(self, v: float, strict: bool) -> None:
        if self.lo is None or v > self.lo or (v == self.lo and strict):
            self.lo, self.lo_strict = v, strict

    def tighten_hi(self, v: float, strict: bool) -> None:
        if self.hi is None or v < self.hi or (v == self.hi and strict):
            self.hi, self.hi_strict = v, strict

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Pow, ast.FloorDiv, ast.Div)


def const_eval(node: ast.AST) -> Optional[float]:
    """Evaluate a *numeric constant* expression without executing code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = const_eval(node.operand)
        if v is None:
            return None
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        a, b = const_eval(node.left), const_eval(node.right)
        if a is None or b is None:
            return None
        if isinstance(node.op, ast.Pow):
            if abs(b) > 256 or abs(a) > 2**16:
                return None
            return a ** b
        if isinstance(node.op, (ast.Div, ast.FloorDiv)) and b == 0:
            return None
        return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b,
                ast.FloorDiv: a // b if isinstance(node.op, ast.FloorDiv) else 0,
                ast.Div: a / b if isinstance(node.op, ast.Div) else 0}[type(node.op)]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "math":
        return {"inf": math.inf, "pi": math.pi, "e": math.e}.get(node.attr)
    return None


ElemVars = Optional[dict[str, str]]  # loop variable -> constraint key (e.g. n -> "runs[*][1]")


def _var_key(node: ast.AST, elem_var: ElemVars) -> Optional[str]:
    if isinstance(node, ast.Name):
        if elem_var and node.id in elem_var:
            return elem_var[node.id]
        return node.id
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len" \
            and len(node.args) == 1 and isinstance(node.args[0], ast.Name):
        return f"len({node.args[0].id})"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "abs" \
            and len(node.args) == 1:
        inner = _var_key(node.args[0], elem_var)
        return f"abs:{inner}" if inner else None
    return None


_FLIP = {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE, ast.Eq: ast.Eq}


class ConstraintSet:
    def __init__(self) -> None:
        self.vars: dict[str, VarConstraint] = {}

    def get(self, key: str) -> VarConstraint:
        return self.vars.setdefault(key, VarConstraint())

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {k: v.to_dict() for k, v in self.vars.items()}

    @staticmethod
    def from_dict(d: dict[str, dict[str, Any]]) -> "ConstraintSet":
        cs = ConstraintSet()
        for k, v in d.items():
            cs.vars[k] = VarConstraint(**v)
        return cs

    # ------------------------------------------------------------------ propagation
    def _bound(self, key: str, op: ast.cmpop, value: float) -> None:
        """Apply ``key <op> value``."""
        absolute = key.startswith("abs:")
        if absolute:
            key = key[4:]
        vc = self.get(key)
        if isinstance(op, (ast.Lt, ast.LtE)):
            strict = isinstance(op, ast.Lt)
            vc.tighten_hi(value, strict)
            if absolute:
                vc.tighten_lo(-value, strict)
        elif isinstance(op, (ast.Gt, ast.GtE)) and not absolute:
            vc.tighten_lo(value, isinstance(op, ast.Gt))
        elif isinstance(op, ast.Eq) and not absolute:
            vc.tighten_lo(value, False)
            vc.tighten_hi(value, False)

    def _compare(self, node: ast.Compare, elem_var: ElemVars) -> None:
        operands = [node.left] + list(node.comparators)
        for left, op, right in zip(operands, node.ops, operands[1:]):
            if type(op) not in _FLIP:
                continue
            lk, rk = _var_key(left, elem_var), _var_key(right, elem_var)
            lv, rv = const_eval(left), const_eval(right)
            if lk and rv is not None:
                self._bound(lk, op, rv)
            elif rk and lv is not None:
                self._bound(rk, _FLIP[type(op)](), lv)
            # sortedness: xs == sorted(xs)
            if isinstance(op, ast.Eq) and isinstance(left, ast.Name) and isinstance(right, ast.Call) \
                    and isinstance(right.func, ast.Name) and right.func.id == "sorted" and right.args \
                    and isinstance(right.args[0], ast.Name) and right.args[0].id == left.id:
                self.get(left.id).sorted = True

    def _isfinite(self, node: ast.AST, elem_var: ElemVars) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "isfinite" \
                and node.args:
            k = _var_key(node.args[0], elem_var)
            if k:
                self.get(k).finite = True
                return True
        return False

    def add_expr(self, node: ast.AST, elem_var: ElemVars = None) -> None:
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            for v in node.values:
                self.add_expr(v, elem_var)
        elif isinstance(node, ast.Compare):
            self._compare(node, elem_var)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "all" \
                and len(node.args) == 1 and isinstance(node.args[0], ast.GeneratorExp):
            gen = node.args[0]
            comp = gen.generators[0] if len(gen.generators) == 1 else None
            if comp is not None and isinstance(comp.iter, ast.Name) and not comp.ifs:
                ev: dict[str, str] = {}
                if isinstance(comp.target, ast.Name):
                    ev[comp.target.id] = f"{comp.iter.id}[*]"
                elif isinstance(comp.target, ast.Tuple):
                    for i, elt in enumerate(comp.target.elts):
                        if isinstance(elt, ast.Name) and elt.id != "_":
                            ev[elt.id] = f"{comp.iter.id}[*][{i}]"
                if ev:
                    self.add_expr(gen.elt, ev)
        else:
            self._isfinite(node, elem_var)


def solve_preconditions(requires: list[str]) -> ConstraintSet:
    cs = ConstraintSet()
    for expr in requires:
        try:
            cs.add_expr(ast.parse(expr, mode="eval").body)
        except SyntaxError:
            continue
    return cs
