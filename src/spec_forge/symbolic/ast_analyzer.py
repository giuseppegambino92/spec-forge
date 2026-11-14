"""Deep static analysis of target modules.

Extracts, per function / class:
  * type specs from annotations (and defaults when annotations are missing),
  * docstring contract clauses (``:requires:``, ``:ensures:``, ``:law:``, ``:raises:`` ...),
  * dataflow facts: parameters mutated in place, mutable defaults, nondeterminism sources,
    global writes, explicitly raised exception types,
  * risk sites the Builder uses as repair anchors (divisions, computed indexing,
    subtractions on ints, float summation, comparisons, unbounded loops),
  * read-modify-write patterns on ``self`` attributes (atomicity hazards).

Everything is pure ``ast`` work: no target code is executed here.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------- type specs


@dataclass
class TypeSpec:
    kind: str                     # int float str bool bytes none list tuple dict set optional any
    args: list["TypeSpec"] = field(default_factory=list)
    variadic: bool = False        # tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "args": [a.to_dict() for a in self.args], "variadic": self.variadic}

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> Optional["TypeSpec"]:
        if d is None:
            return None
        return cls(d["kind"], [cls.from_dict(a) for a in d.get("args", [])], d.get("variadic", False))  # type: ignore[misc]

    def __str__(self) -> str:
        if not self.args:
            return self.kind
        inner = ", ".join(str(a) for a in self.args) + (", ..." if self.variadic else "")
        return f"{self.kind}[{inner}]"


_SCALARS = {"int": "int", "float": "float", "str": "str", "bool": "bool", "bytes": "bytes", "None": "none",
            "complex": "float", "Decimal": "float"}
_SEQ = {"list", "List", "Sequence", "Iterable", "MutableSequence", "Collection", "deque", "Deque", "Iterator"}
_MAP = {"dict", "Dict", "Mapping", "MutableMapping", "defaultdict", "OrderedDict", "Counter"}
_SET = {"set", "Set", "frozenset", "FrozenSet", "AbstractSet"}


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def parse_type(node: Optional[ast.AST]) -> TypeSpec:
    if node is None:
        return TypeSpec("any")
    if isinstance(node, ast.Constant):
        if node.value is None:
            return TypeSpec("none")
        if isinstance(node.value, str):  # string annotation
            try:
                return parse_type(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return TypeSpec("any")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = parse_type(node.left), parse_type(node.right)
        if right.kind == "none":
            return TypeSpec("optional", [left])
        if left.kind == "none":
            return TypeSpec("optional", [right])
        return left
    name = _name_of(node)
    if name in _SCALARS:
        return TypeSpec(_SCALARS[name])
    if name in _SEQ:
        return TypeSpec("list", [TypeSpec("int")])
    if name in _MAP:
        return TypeSpec("dict", [TypeSpec("str"), TypeSpec("int")])
    if name in _SET:
        return TypeSpec("set", [TypeSpec("int")])
    if name in ("tuple", "Tuple"):
        return TypeSpec("tuple", [TypeSpec("int")], variadic=True)
    if isinstance(node, ast.Subscript):
        base = _name_of(node.value)
        sl = node.slice
        elts = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
        if base in _SEQ:
            return TypeSpec("list", [parse_type(elts[0])])
        if base in _SET:
            return TypeSpec("set", [parse_type(elts[0])])
        if base in _MAP:
            return TypeSpec("dict", [parse_type(elts[0]), parse_type(elts[1] if len(elts) > 1 else None)])
        if base in ("tuple", "Tuple"):
            if len(elts) == 2 and isinstance(elts[1], ast.Constant) and elts[1].value is Ellipsis:
                return TypeSpec("tuple", [parse_type(elts[0])], variadic=True)
            return TypeSpec("tuple", [parse_type(e) for e in elts])
        if base == "Optional":
            return TypeSpec("optional", [parse_type(elts[0])])
        if base == "Union":
            non_none = [parse_type(e) for e in elts if not (isinstance(e, ast.Constant) and e.value is None)]
            has_none = len(non_none) < len(elts)
            t = non_none[0] if non_none else TypeSpec("none")
            return TypeSpec("optional", [t]) if has_none else t
    return TypeSpec("any")


def type_from_default(node: Optional[ast.AST]) -> TypeSpec:
    if node is None:
        return TypeSpec("any")
    if isinstance(node, ast.Constant):
        v = node.value
        for py, k in ((bool, "bool"), (int, "int"), (float, "float"), (str, "str"), (bytes, "bytes")):
            if isinstance(v, py):
                return TypeSpec(k)
        if v is None:
            return TypeSpec("optional", [TypeSpec("int")])
    if isinstance(node, ast.List):
        return TypeSpec("list", [TypeSpec("int")])
    if isinstance(node, ast.Dict):
        return TypeSpec("dict", [TypeSpec("str"), TypeSpec("int")])
    if isinstance(node, ast.Set):
        return TypeSpec("set", [TypeSpec("int")])
    return TypeSpec("any")


# --------------------------------------------------------------------------- contracts

CLAUSE_KEYS = ("requires", "ensures", "invariant", "law", "raises", "idempotent", "pure", "inverse",
               "thread_safe", "order_independent", "deterministic")
EXPR_CLAUSES = {"requires", "ensures", "invariant", "law"}


@dataclass
class ContractClauses:
    requires: list[str] = field(default_factory=list)
    ensures: list[str] = field(default_factory=list)
    laws: list[str] = field(default_factory=list)
    raises: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)  # idempotent / pure / inverse / thread_safe ...
    parse_errors: list[str] = field(default_factory=list)

    def all_expressions(self) -> list[tuple[str, str]]:
        return ([("requires", e) for e in self.requires] + [("ensures", e) for e in self.ensures]
                + [("law", e) for e in self.laws])


def _parses(expr: str) -> bool:
    try:
        ast.parse(expr.strip(), mode="eval")
        return True
    except SyntaxError:
        return False


def parse_contract(doc: Optional[str]) -> ContractClauses:
    out = ContractClauses()
    if not doc:
        return out
    pending: Optional[tuple[str, str]] = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        key, text = pending
        text = text.strip()
        pending = None
        if key in EXPR_CLAUSES:
            if not _parses(text):
                out.parse_errors.append(f":{key}: {text}")
                return
            if key == "requires":
                out.requires.append(text)
            elif key in ("ensures", "invariant"):
                out.ensures.append(text)
            else:
                out.laws.append(text)
        elif key == "raises":
            out.raises.extend(t.strip() for t in text.replace(";", ",").split(",") if t.strip())
        else:
            out.flags[key] = text

    for raw in doc.splitlines():
        line = raw.strip()
        matched = False
        for key in CLAUSE_KEYS:
            tag = f":{key}:"
            if line.startswith(tag):
                flush()
                pending = (key, line[len(tag):].strip())
                matched = True
                break
        if matched:
            continue
        if pending is not None:
            key, text = pending
            # continuation lines extend an expression that does not parse yet
            if line and key in EXPR_CLAUSES and not _parses(text.strip()):
                pending = (key, text + " " + line)
                continue
            flush()
    flush()
    return out


# --------------------------------------------------------------------------- facts

MUTATING_METHODS = {"append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse", "update",
                    "setdefault", "popitem", "add", "discard", "difference_update", "intersection_update",
                    "symmetric_difference_update", "appendleft", "popleft", "extendleft", "rotate"}
NONDET_MODULES = {"random", "secrets", "uuid", "time", "datetime"}
NONDET_CALLS = {"id", "hash", "urandom", "now", "utcnow", "today", "random", "randint", "choice", "shuffle",
                "uuid4", "time", "monotonic", "perf_counter"}


@dataclass
class ParamFact:
    name: str
    type: TypeSpec
    has_default: bool = False
    default_src: Optional[str] = None
    default_is_mutable: bool = False
    kind: str = "positional"      # positional | kwonly | varargs | varkw

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.to_dict()
        return d


@dataclass
class RiskSite:
    kind: str     # division | index | subtraction | float_sum | comparison | loop | slice
    line: int
    detail: str


@dataclass
class FunctionFacts:
    name: str
    lineno: int
    end_lineno: int
    params: list[ParamFact]
    returns: TypeSpec
    returns_value: bool
    docstring: Optional[str]
    clauses: ContractClauses
    mutated_params: list[str] = field(default_factory=list)
    nondeterministic: bool = False
    writes_globals: bool = False
    explicit_raises: list[str] = field(default_factory=list)
    risk_sites: list[RiskSite] = field(default_factory=list)
    is_method: bool = False
    class_name: Optional[str] = None
    self_reads: list[str] = field(default_factory=list)
    self_writes: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    @property
    def public(self) -> bool:
        return not self.name.startswith("_")

    @property
    def rmw_attrs(self) -> list[str]:
        return sorted(set(self.self_reads) & set(self.self_writes))


@dataclass
class ClassFacts:
    name: str
    lineno: int
    end_lineno: int
    docstring: Optional[str]
    clauses: ContractClauses
    methods: list[FunctionFacts]
    init_params: list[ParamFact]
    mentions_concurrency: bool

    @property
    def public(self) -> bool:
        return not self.name.startswith("_")


@dataclass
class ModuleFacts:
    functions: list[FunctionFacts]
    classes: list[ClassFacts]
    imports: list[str]
    tree: ast.Module

    def function(self, name: str) -> Optional[FunctionFacts]:
        return next((f for f in self.functions if f.name == name), None)

    def klass(self, name: str) -> Optional[ClassFacts]:
        return next((c for c in self.classes if c.name == name), None)


def _is_mutable_default(node: Optional[ast.AST]) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call) and _name_of(node.func) in {"list", "dict", "set", "defaultdict", "deque",
                                                              "OrderedDict", "Counter", "bytearray"}:
        return True
    return False


def _root_name(node: ast.AST) -> Optional[str]:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _FunctionVisitor(ast.NodeVisitor):
    def __init__(self, params: set[str], returns_float: bool, returns_int: bool) -> None:
        self.params = params
        self.returns_float = returns_float
        self.returns_int = returns_int
        self.mutated: set[str] = set()
        self.nondet = False
        self.writes_globals = False
        self.raises: set[str] = set()
        self.risks: list[RiskSite] = []
        self.self_reads: set[str] = set()
        self.self_writes: set[str] = set()
        self.returns_value = False
        self.calls: set[str] = set()
        self._depth = 0

    # nested defs: analyse bodies too (closures share params) but keep returns of outer only
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_Return(self, node: ast.Return) -> None:
        if self._depth == 0 and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            self.returns_value = True
            if self.returns_int:
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Sub):
                        self.risks.append(RiskSite("subtraction", sub.lineno, ast.unparse(sub)))
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.writes_globals = True

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.writes_globals = True

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            n = _name_of(target)
            if n:
                self.raises.add(n)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        fname = _name_of(node.func)
        self.calls.add(fname)
        if isinstance(node.func, ast.Attribute):
            root = _root_name(node.func.value)
            if node.func.attr in MUTATING_METHODS and root in self.params:
                self.mutated.add(root)
            if root in NONDET_MODULES and node.func.attr != "sleep":
                self.nondet = True
        if fname in {"urandom", "uuid4", "now", "utcnow", "randint", "random", "shuffle", "choice"}:
            self.nondet = True
        if fname == "sum" and self.returns_float:
            self.risks.append(RiskSite("float_sum", node.lineno, ast.unparse(node)))
        self.generic_visit(node)

    def _store_target(self, t: ast.AST) -> None:
        if isinstance(t, (ast.Subscript, ast.Attribute)):
            root = _root_name(t)
            if root in self.params and root != "self":
                self.mutated.add(root)
        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
            self.self_writes.add(t.attr)
        if isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._store_target(e)

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            self._store_target(t)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._store_target(node.target)
        if isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name) \
                and node.target.value.id == "self":
            self.self_reads.add(node.target.attr)
        if isinstance(node.target, ast.Name) and node.target.id in self.params and isinstance(node.op, ast.Add):
            pass  # rebinding a local name; list += mutates in place but we treat conservatively below
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for t in node.targets:
            self._store_target(t)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name) and node.value.id == "self":
            self.self_reads.add(node.attr)
        if isinstance(node.value, ast.Name) and node.value.id in NONDET_MODULES and node.attr not in {"sleep"}:
            if node.value.id in {"random", "secrets", "uuid"}:
                self.nondet = True
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
            self.risks.append(RiskSite("division", node.lineno, ast.unparse(node)))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load):
            if isinstance(node.slice, ast.Slice):
                self.risks.append(RiskSite("slice", node.lineno, ast.unparse(node)))
            elif not isinstance(node.slice, ast.Constant):
                self.risks.append(RiskSite("index", node.lineno, ast.unparse(node)))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        self.risks.append(RiskSite("comparison", node.lineno, ast.unparse(node)))
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.risks.append(RiskSite("loop", node.lineno, ast.unparse(node.test)))
        self.generic_visit(node)


def _params_of(fn: ast.FunctionDef, skip_self: bool) -> list[ParamFact]:
    a = fn.args
    out: list[ParamFact] = []
    positional = list(a.posonlyargs) + list(a.args)
    defaults: list[Optional[ast.expr]] = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    for arg, d in zip(positional, defaults):
        out.append(_param(arg, d, "positional"))
    if a.vararg:
        out.append(ParamFact(a.vararg.arg, TypeSpec("list", [parse_type(a.vararg.annotation)]), kind="varargs"))
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        out.append(_param(arg, d, "kwonly"))
    if a.kwarg:
        out.append(ParamFact(a.kwarg.arg, TypeSpec("dict", [TypeSpec("str"), parse_type(a.kwarg.annotation)]),
                             kind="varkw"))
    if skip_self and out and out[0].name in ("self", "cls"):
        out = out[1:]
    return out


def _param(arg: ast.arg, default: Optional[ast.expr], kind: str) -> ParamFact:
    t = parse_type(arg.annotation) if arg.annotation is not None else type_from_default(default)
    if t.kind == "any":
        t = TypeSpec("int")
    return ParamFact(
        name=arg.arg, type=t, has_default=default is not None,
        default_src=ast.unparse(default) if default is not None else None,
        default_is_mutable=_is_mutable_default(default), kind=kind,
    )


def analyze_function(fn: ast.FunctionDef, class_name: Optional[str] = None) -> FunctionFacts:
    params = _params_of(fn, skip_self=class_name is not None)
    returns = parse_type(fn.returns) if fn.returns is not None else TypeSpec("any")
    rfloat = returns.kind == "float" or (returns.kind == "list" and returns.args and returns.args[0].kind == "float")
    names = {p.name for p in params} | ({"self"} if class_name else set())
    v = _FunctionVisitor(names, returns_float=bool(rfloat), returns_int=returns.kind == "int")
    for stmt in fn.body:
        v.visit(stmt)
    doc = ast.get_docstring(fn)
    return FunctionFacts(
        name=fn.name, lineno=fn.lineno, end_lineno=fn.end_lineno or fn.lineno, params=params, returns=returns,
        returns_value=v.returns_value, docstring=doc, clauses=parse_contract(doc),
        mutated_params=sorted(p for p in v.mutated if p != "self"), nondeterministic=v.nondet,
        writes_globals=v.writes_globals, explicit_raises=sorted(v.raises), risk_sites=v.risks,
        is_method=class_name is not None, class_name=class_name,
        self_reads=sorted(v.self_reads), self_writes=sorted(v.self_writes), calls=sorted(v.calls),
    )


_CONCURRENCY_WORDS = ("thread", "concurren", "parallel", "worker", "shared", "atomic", "race")


def analyze_module(source: str) -> ModuleFacts:
    tree = ast.parse(source)
    functions: list[FunctionFacts] = []
    classes: list[ClassFacts] = []
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(analyze_function(node))  # type: ignore[arg-type]
        elif isinstance(node, ast.ClassDef):
            methods = [analyze_function(m, node.name) for m in node.body  # type: ignore[arg-type]
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            init = next((m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)
            doc = ast.get_docstring(node)
            text = (doc or "").lower()
            classes.append(ClassFacts(
                name=node.name, lineno=node.lineno, end_lineno=node.end_lineno or node.lineno, docstring=doc,
                clauses=parse_contract(doc), methods=methods,
                init_params=_params_of(init, skip_self=True) if init else [],
                mentions_concurrency=any(w in text for w in _CONCURRENCY_WORDS),
            ))
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    return ModuleFacts(functions=functions, classes=classes, imports=imports, tree=tree)


def function_node(tree: ast.Module, name: str, class_name: Optional[str] = None) -> Optional[ast.FunctionDef]:
    body = tree.body
    if class_name:
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
        if cls is None:
            return None
        body = cls.body
    return next((n for n in body if isinstance(n, ast.FunctionDef) and n.name == name), None)
