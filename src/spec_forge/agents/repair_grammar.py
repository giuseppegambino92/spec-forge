"""AST transformation grammar for program repair.

Each rule inspects the AST of the current candidate and yields concrete ``Candidate`` edits.
Edits are applied *textually* at the exact node span (``lineno/col_offset`` ..
``end_lineno/end_col_offset``, UTF-8 byte columns) so every patch is a minimal, reviewable
diff rather than an ``ast.unparse`` reformat of the whole module.

Local rules (anchored at a line, ranked by fault-localisation suspiciousness)
  compare_swap       < <-> <=, > <-> >=, == <-> !=          (boundary / off-by-one conditions)
  const_perturb      integer literal +-1                     (off-by-one constants)
  arith_nudge        e -> e + 1 / e - 1 on +,- expressions, slice bounds, range() args
  binop_swap         + <-> -, / <-> //
  zero_div_guard     a / b -> (a / b if b != 0 else 0.0)
  index_clamp        xs[i] -> xs[min(max(i, 0), len(xs) - 1)]
  saturating_sub     a - b -> max(0, a - b)
  exact_sum          sum(xs) -> math.fsum(xs)                (floating-point drift)
                     for x in xs: t += x  ->  t += math.fsum(xs)
Function-level templates (anchored at the target definition)
  contract_clamp         clamp returned value into the bounds stated by ``:ensures:``
  conservation_rebalance distribute the residual of ``sum(result) == E`` one unit at a time
  defensive_copy         copy mutable parameters on entry (aliasing / input mutation)
  mutable_default        ``p=[]`` -> ``p=None`` + ``if p is None: p = []``
  fixpoint_closure       iterate the transformation to a fixpoint (idempotency)
  lock_isolation         guard read-modify-write methods with a per-instance RLock (atomicity)
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

from ..symbolic.ast_analyzer import analyze_function, parse_type


@dataclass
class Candidate:
    category: str
    description: str
    line: int
    source: str
    local: bool = True
    prior: float = 0.0
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- text patching


class SourceText:
    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self._starts = [0]
        for ln in self.lines:
            self._starts.append(self._starts[-1] + len(ln))

    def offset(self, lineno: int, col_byte: int) -> int:
        line = self.lines[lineno - 1] if lineno - 1 < len(self.lines) else ""
        prefix = line.encode("utf-8")[:col_byte].decode("utf-8", errors="ignore")
        return self._starts[lineno - 1] + len(prefix)

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (self.offset(node.lineno, node.col_offset),  # type: ignore[attr-defined]
                self.offset(node.end_lineno, node.end_col_offset))  # type: ignore[attr-defined]

    def segment(self, node: ast.AST) -> str:
        a, b = self.span(node)
        return self.source[a:b]

    def replace(self, node: ast.AST, text: str) -> str:
        a, b = self.span(node)
        return self.source[:a] + text + self.source[b:]

    def indent_of(self, lineno: int) -> str:
        line = self.lines[lineno - 1]
        return line[: len(line) - len(line.lstrip())]


def _valid(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def _ensure_import(src: str, module: str) -> str:
    if re.search(rf"^\s*import\s+{module}\b", src, re.M):
        return src
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    insert_at = 0
    for node in tree.body:
        is_doc = isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
            and isinstance(node.value.value, str)
        if is_doc or isinstance(node, (ast.Import, ast.ImportFrom)):
            insert_at = node.end_lineno or insert_at
        else:
            break
    lines.insert(insert_at, f"import {module}\n")
    return "".join(lines)


def _body_start(fn: ast.FunctionDef) -> int:
    """Index of the first non-docstring statement."""
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) \
            and isinstance(fn.body[0].value.value, str):
        return 1
    return 0


def _insert_at_body_start(st: SourceText, fn: ast.FunctionDef, stmts: list[str]) -> str:
    idx = _body_start(fn)
    if idx < len(fn.body):
        anchor = fn.body[idx]
        indent = st.indent_of(anchor.lineno)
        pos = st.offset(anchor.lineno, 0)
    else:
        last = fn.body[-1]
        indent = st.indent_of(last.lineno)
        pos = st.offset(last.end_lineno + 1, 0) if last.end_lineno < len(st.lines) else len(st.source)  # type: ignore[operator]
    text = "".join(f"{indent}{s}\n" for s in stmts)
    return st.source[:pos] + text + st.source[pos:]


# --------------------------------------------------------------------------- local rules

_CMP_ALT = {ast.Lt: [ast.LtE], ast.LtE: [ast.Lt], ast.Gt: [ast.GtE], ast.GtE: [ast.Gt], ast.Eq: [ast.NotEq],
            ast.NotEq: [ast.Eq]}
_CMP_TXT = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}


def _in_scope(node: ast.AST, scopes: list[tuple[int, int]]) -> bool:
    ln = getattr(node, "lineno", None)
    return ln is not None and any(a <= ln <= b for a, b in scopes)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def local_candidates(src: str, scopes: list[tuple[int, int]], skip_lines: set[int]) -> Iterator[Candidate]:
    tree = ast.parse(src)
    st = SourceText(src)
    docs = _docstring_nodes(tree)
    defaults: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in [*n.args.defaults, *n.args.kw_defaults]:
                if d is not None:
                    defaults.update(id(x) for x in ast.walk(d))
            for d in n.decorator_list:
                defaults.update(id(x) for x in ast.walk(d))
    for node in ast.walk(tree):
        if not _in_scope(node, scopes) or id(node) in docs or id(node) in defaults:
            continue
        line = node.lineno  # type: ignore[attr-defined]
        if line in skip_lines:
            continue
        seg = None
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                for alt in _CMP_ALT.get(type(op), []):
                    new = ast.Compare(left=node.left, ops=list(node.ops), comparators=node.comparators)
                    new.ops[i] = alt()
                    text = ast.unparse(new)
                    yield Candidate("compare_swap", f"L{line}: `{_CMP_TXT[type(op)]}` -> `{_CMP_TXT[alt]}` in "
                                    f"`{ast.unparse(node)}`", line, st.replace(node, text))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            for d in (1, -1):
                v = node.value + d
                if v < 0 and node.value >= 0:
                    continue
                yield Candidate("const_perturb", f"L{line}: literal {node.value} -> {v}", line,
                                st.replace(node, str(v)))
        elif isinstance(node, ast.BinOp):
            seg = st.segment(node)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                for d in ("+ 1", "- 1"):
                    yield Candidate("arith_nudge", f"L{line}: `{seg}` -> `{seg} {d}`", line,
                                    st.replace(node, f"({seg} {d})" if _needs_paren(node) else f"{seg} {d}"))
                swapped = ast.BinOp(left=node.left, op=ast.Sub() if isinstance(node.op, ast.Add) else ast.Add(),
                                    right=node.right)
                yield Candidate("binop_swap", f"L{line}: `{seg}` -> `{ast.unparse(swapped)}`", line,
                                st.replace(node, ast.unparse(swapped)))
                if isinstance(node.op, ast.Sub):
                    yield Candidate("saturating_sub", f"L{line}: `{seg}` -> `max(0, {seg})`", line,
                                    st.replace(node, f"max(0, {seg})"))
            if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
                l_txt, r_txt = st.segment(node.left), st.segment(node.right)
                fallback = "0.0" if isinstance(node.op, ast.Div) else "0"
                guard = f"({seg} if {r_txt} != 0 else {fallback})"
                yield Candidate("zero_div_guard", f"L{line}: guard divisor `{r_txt}` in `{seg}`", line,
                                st.replace(node, guard))
                other = ast.FloorDiv() if isinstance(node.op, ast.Div) else ast.Div()
                if not isinstance(node.op, ast.Mod):
                    yield Candidate("binop_swap", f"L{line}: `{seg}` -> `{l_txt} {'//' if isinstance(other, ast.FloorDiv) else '/'} {r_txt}`",
                                    line, st.replace(node, ast.unparse(ast.BinOp(node.left, other, node.right))))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if isinstance(node.slice, ast.Slice):
                for bound in (node.slice.lower, node.slice.upper):
                    if bound is None:
                        continue
                    b = st.segment(bound)
                    for d in ("+ 1", "- 1"):
                        yield Candidate("arith_nudge", f"L{line}: slice bound `{b}` -> `{b} {d}`", line,
                                        st.replace(bound, f"{b} {d}"))
            elif not isinstance(node.slice, ast.Constant):
                seq, idx = st.segment(node.value), st.segment(node.slice)
                yield Candidate("index_clamp", f"L{line}: clamp index `{idx}` into bounds of `{seq}`", line,
                                st.replace(node.slice, f"min(max({idx}, 0), len({seq}) - 1)"))
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name) and not node.orelse \
                and len(node.body) == 1 and isinstance(node.body[0], ast.AugAssign) \
                and isinstance(node.body[0].op, ast.Add) and isinstance(node.body[0].target, ast.Name) \
                and isinstance(node.body[0].value, ast.Name) and node.body[0].value.id == node.target.id:
            # A running `+=` total rounds after every step, so its result depends on the order.
            acc, seq = node.body[0].target.id, st.segment(node.iter)
            new_src = st.replace(node, f"{acc} += math.fsum({seq})")
            yield Candidate("exact_sum", f"L{line}: running total `{acc} += {node.target.id}` -> "
                            f"`{acc} += math.fsum({seq})` (exactly rounded)", line, _ensure_import(new_src, "math"))
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name == "sum" and len(node.args) == 1 and not node.keywords:
                arg = st.segment(node.args[0])
                new_src = st.replace(node, f"math.fsum({arg})")
                yield Candidate("exact_sum", f"L{line}: `sum({arg})` -> `math.fsum({arg})` (exactly rounded)", line,
                                _ensure_import(new_src, "math"))
            if name == "range":
                for a in node.args:
                    b = st.segment(a)
                    for d in ("+ 1", "- 1"):
                        yield Candidate("arith_nudge", f"L{line}: range bound `{b}` -> `{b} {d}`", line,
                                        st.replace(a, f"{b} {d}"))


def _needs_paren(node: ast.AST) -> bool:
    return False


# --------------------------------------------------------------------------- templates


def _find_fn(tree: ast.Module, name: str, class_name: Optional[str]) -> Optional[ast.FunctionDef]:
    body = tree.body
    if class_name:
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
        body = cls.body if cls else []
    return next((n for n in body if isinstance(n, ast.FunctionDef) and n.name == name), None)


def _returns(fn: ast.FunctionDef) -> list[ast.Return]:
    out: list[ast.Return] = []
    stack: list[ast.AST] = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Return) and n.value is not None:
            out.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))
    return sorted(out, key=lambda r: -r.lineno)  # bottom-up so earlier spans stay valid


def _bounds_from_ensures(ensures: list[str]) -> tuple[Optional[str], Optional[str]]:
    lo = hi = None
    for e in ensures:
        try:
            node = ast.parse(e, mode="eval").body
        except SyntaxError:
            continue
        if not isinstance(node, ast.Compare):
            continue
        ops = [node.left] + list(node.comparators)
        for left, op, right in zip(ops, node.ops, ops[1:]):
            if isinstance(right, ast.Name) and right.id == "result":
                if isinstance(op, (ast.LtE, ast.Lt)):
                    lo = ast.unparse(left)
                elif isinstance(op, (ast.GtE, ast.Gt)):
                    hi = ast.unparse(left)
            if isinstance(left, ast.Name) and left.id == "result":
                if isinstance(op, (ast.LtE, ast.Lt)):
                    hi = ast.unparse(right)
                elif isinstance(op, (ast.GtE, ast.Gt)):
                    lo = ast.unparse(right)
    return lo, hi


def _conservation_target(ensures: list[str]) -> Optional[str]:
    for e in ensures:
        try:
            node = ast.parse(e, mode="eval").body
        except SyntaxError:
            continue
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            l, r = node.left, node.comparators[0]
            for a, b in ((l, r), (r, l)):
                if isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "sum" and a.args \
                        and isinstance(a.args[0], ast.Name) and a.args[0].id == "result":
                    return ast.unparse(b)
    return None


def template_candidates(src: str, target: str, class_name: Optional[str], method: Optional[str],
                        ensures: list[str], rmw_attrs: list[str]) -> Iterator[Candidate]:
    tree = ast.parse(src)
    st = SourceText(src)
    if class_name:
        yield from _lock_isolation(src, tree, class_name, method, rmw_attrs)
        return
    fn = _find_fn(tree, target, None)
    if fn is None:
        return
    facts = analyze_function(fn)

    # contract_clamp
    lo, hi = _bounds_from_ensures(ensures)
    if lo or hi:
        new = src
        for r in _returns(_find_fn(ast.parse(new), target, None)):  # type: ignore[arg-type]
            s2 = SourceText(new)
            v = s2.segment(r.value)  # type: ignore[arg-type]
            expr = v
            if lo:
                expr = f"max({lo}, {expr})"
            if hi:
                expr = f"min({hi}, {expr})"
            new = s2.replace(r.value, expr)  # type: ignore[arg-type]
        if new != src and _valid(new):
            yield Candidate("contract_clamp", f"clamp return of {target}() into [{lo or '-inf'}, {hi or '+inf'}] "
                            "derived from :ensures:", fn.lineno, new, local=False)

    # conservation_rebalance
    total = _conservation_target(ensures)
    if total and facts.returns.kind in ("list", "any"):
        new = src
        for r in _returns(_find_fn(ast.parse(new), target, None)):  # type: ignore[arg-type]
            s2 = SourceText(new)
            v = s2.segment(r.value)  # type: ignore[arg-type]
            ind = s2.indent_of(r.lineno)
            block = (f"_sf_res = list({v})\n"
                     f"{ind}_sf_residual = ({total}) - sum(_sf_res)\n"
                     f"{ind}_sf_step = 1 if _sf_residual > 0 else -1\n"
                     f"{ind}for _sf_i in range(abs(int(_sf_residual)) if _sf_res else 0):\n"
                     f"{ind}    _sf_res[_sf_i % len(_sf_res)] += _sf_step\n"
                     f"{ind}return _sf_res")
            new = s2.replace(r, block)
        if new != src and _valid(new):
            yield Candidate("conservation_rebalance", f"rebalance residual so `sum(result) == {total}` "
                            "(unit-wise distribution)", fn.lineno, new, local=False)

    # defensive_copy
    copies = []
    for p in facts.params:
        if p.type.kind in ("list", "dict", "set"):
            ctor = {"list": "list", "dict": "dict", "set": "set"}[p.type.kind]
            copies.append(f"{p.name} = {ctor}({p.name})")
    if copies:
        new = _insert_at_body_start(st, fn, copies)
        if _valid(new):
            yield Candidate("defensive_copy", f"copy mutable parameter(s) on entry: {'; '.join(copies)}", fn.lineno,
                            new, local=False)

    # mutable_default
    a = fn.args
    positional = list(a.posonlyargs) + list(a.args)
    pairs = list(zip(positional[len(positional) - len(a.defaults):], a.defaults)) + \
        [(k, d) for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
    mut = [(arg, d) for arg, d in pairs if isinstance(d, (ast.List, ast.Dict, ast.Set)) or (
        isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id in ("list", "dict", "set"))]
    if mut:
        new = src
        inits = []
        for arg, d in sorted(mut, key=lambda x: (-x[1].lineno, -x[1].col_offset)):
            s2 = SourceText(new)
            inits.append(f"if {arg.arg} is None:\n    {arg.arg} = {s2.segment(d)}")
            new = s2.replace(d, "None")
        fn2 = _find_fn(ast.parse(new), target, None)
        stmts: list[str] = []
        for block in inits:
            head, body = block.split("\n")
            stmts.append(head)
            stmts.append(body)
        new = _insert_at_body_start(SourceText(new), fn2, stmts)  # type: ignore[arg-type]
        # fix indentation of the assignment lines (inserted with the same indent as the `if`)
        new = re.sub(r"^(\s*)if (\w+) is None:\n\1    (\w+) = ", lambda m: m.group(0), new, flags=re.M)
        if _valid(new):
            yield Candidate("mutable_default", "replace mutable default(s) with None sentinel: "
                            + ", ".join(arg.arg for arg, _ in mut), fn.lineno, new, local=False)

    # fixpoint_closure (idempotency)
    if facts.params and facts.returns_value:
        yield from _fixpoint(src, st, fn)


def _fixpoint(src: str, st: SourceText, fn: ast.FunctionDef) -> Iterator[Candidate]:
    start = _body_start(fn)
    if start >= len(fn.body):
        return
    first = fn.body[start]
    last = fn.body[-1]
    body_lines = st.lines[first.lineno - 1: last.end_lineno]  # type: ignore[misc]
    ind = st.indent_of(first.lineno)
    params = fn.args
    sig = ast.unparse(params)
    pos_names = [a.arg for a in params.posonlyargs + params.args]
    kw_names = [a.arg for a in params.kwonlyargs]
    first_name = pos_names[0] if pos_names else None
    if first_name is None:
        return

    def call(first_val: str) -> str:
        args = [first_val] + pos_names[1:]
        if params.vararg:
            args.append(f"*{params.vararg.arg}")
        args += [f"{k}={k}" for k in kw_names]
        if params.kwarg:
            args.append(f"**{params.kwarg.arg}")
        return f"_sf_step({', '.join(args)})"

    inner = "".join("    " + ln if ln.strip() else ln for ln in body_lines)
    if not inner.endswith("\n"):
        inner += "\n"
    block = (f"{ind}def _sf_step({sig}):\n{inner}"
             f"{ind}_sf_cur = {call(first_name)}\n"
             f"{ind}for _sf_iter in range(64):\n"
             f"{ind}    _sf_next = {call('_sf_cur')}\n"
             f"{ind}    if _sf_next == _sf_cur:\n"
             f"{ind}        break\n"
             f"{ind}    _sf_cur = _sf_next\n"
             f"{ind}return _sf_cur\n")
    a = st.offset(first.lineno, 0)
    b = st.offset(last.end_lineno + 1, 0) if last.end_lineno < len(st.lines) else len(src)  # type: ignore[operator]
    new = src[:a] + block + src[b:]
    if _valid(new):
        yield Candidate("fixpoint_closure", f"iterate {fn.name}() to a fixpoint (idempotent closure, <=64 steps)",
                        fn.lineno, new, local=False)


def _lock_isolation(src: str, tree: ast.Module, class_name: str, method: Optional[str],
                    rmw_attrs: list[str]) -> Iterator[Candidate]:
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if cls is None:
        return
    methods = [m for m in cls.body if isinstance(m, ast.FunctionDef)]
    init = next((m for m in methods if m.name == "__init__"), None)
    touched = []
    for m in methods:
        if m.name == "__init__":
            continue
        f = analyze_function(m, class_name)
        if m.name == method or set(rmw_attrs) & (set(f.self_reads) | set(f.self_writes)):
            touched.append(m)
    if not touched or init is None:
        return
    new = src
    # wrap bodies bottom-up so earlier offsets stay valid
    for m in sorted(touched, key=lambda x: -x.lineno):
        m2 = _find_fn(ast.parse(new), m.name, class_name)
        assert m2 is not None
        s2 = SourceText(new)
        start = _body_start(m2)
        if start >= len(m2.body):
            continue
        first, last = m2.body[start], m2.body[-1]
        ind = s2.indent_of(first.lineno)
        lines = s2.lines[first.lineno - 1: last.end_lineno]  # type: ignore[misc]
        body = "".join("    " + ln if ln.strip() else ln for ln in lines)
        if not body.endswith("\n"):
            body += "\n"
        a = s2.offset(first.lineno, 0)
        b = s2.offset(last.end_lineno + 1, 0) if last.end_lineno < len(s2.lines) else len(new)  # type: ignore[operator]
        new = new[:a] + f"{ind}with self._sf_lock:\n" + body + new[b:]
    init2 = _find_fn(ast.parse(new), "__init__", class_name)
    assert init2 is not None
    s3 = SourceText(new)
    new = _insert_at_body_start(s3, init2, ["self._sf_lock = threading.RLock()"])
    new = _ensure_import(new, "threading")
    if _valid(new):
        yield Candidate("lock_isolation", f"serialise {', '.join(m.name + '()' for m in touched)} of {class_name} "
                        "with a per-instance RLock", cls.lineno, new, local=False)


def parse_ok(src: str) -> bool:
    return _valid(src)


__all__ = ["Candidate", "SourceText", "local_candidates", "template_candidates", "parse_ok", "parse_type"]
