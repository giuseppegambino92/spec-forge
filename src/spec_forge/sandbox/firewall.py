"""Static AST taint & capability firewall.

Untrusted source (and every contract expression embedded in its docstrings) is parsed and
walked once; nothing is executed. The policy is *default-deny for capabilities*:

  * imports outside an allowlist (os, subprocess, socket, ctypes, importlib, pickle ... are not on it)
  * dynamic-code / reflection builtins (eval, exec, compile, __import__, open, globals, vars, getattr
    with non-literal or dunder names, breakpoint, input ...)
  * dunder & frame attribute traversal (__class__, __subclasses__, __globals__, f_back, gi_frame ...)
  * string-encoded traversal (``"{0.__class__}".format(x)``, ``x["__builtins__"]``)
  * resource exhaustion literals (``while True`` without exit, 10**10**10, [0]*10**10, range(10**12))
  * prompt-injection text in docstrings / comments / string literals (they feed the Spec agent and
    optional LLM policy, so they are an instruction channel)
  * size / AST-node limits

Static analysis is one layer: verification also runs in a resource-capped subprocess with
restricted builtins and a guarded importer (see ``runner.py``).
"""

from __future__ import annotations

import ast
import io
import re
import time
import tokenize
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..config import SandboxPolicy
from ..symbolic.ast_analyzer import parse_contract
from ..symbolic.constraints import const_eval

CRITICAL, HIGH, MEDIUM = "critical", "high", "medium"

BLOCKED_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "globals", "locals", "vars",
    "help", "exit", "quit", "memoryview", "setattr", "delattr", "__build_class__", "copyright", "credits",
    "license",
}
REFLECTIVE_GETTERS = {"getattr", "hasattr"}
DANGEROUS_ATTRS = {
    "__class__", "__base__", "__bases__", "__mro__", "__subclasses__", "__globals__", "__builtins__",
    "__code__", "__closure__", "__dict__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__",
    "__reduce__", "__reduce_ex__", "__loader__", "__spec__", "__import__", "__self__", "__func__",
    "__wrapped__", "__init_subclass__", "__set_name__", "__defaults__", "__kwdefaults__", "__traceback__",
    "f_globals", "f_locals", "f_back", "f_builtins", "f_code", "gi_frame", "gi_code", "cr_frame", "cr_code",
    "ag_frame", "ag_code", "tb_frame", "tb_next", "co_code", "func_globals", "mro", "_getframe",
    "system", "popen", "spawn", "fork", "execv", "execve", "load_module", "exec_module",
}
SAFE_DUNDER_ATTRS = {
    "__init__", "__name__", "__doc__", "__qualname__", "__len__", "__iter__", "__next__", "__repr__",
    "__str__", "__eq__", "__hash__", "__lt__", "__le__", "__gt__", "__ge__", "__enter__", "__exit__",
    "__post_init__", "__slots__", "__contains__", "__getitem__", "__add__",
}
DANGEROUS_NAMES = {"__builtins__", "__import__", "__loader__", "__spec__", "__build_class__"}

INJECTION_PATTERNS = [
    (re.compile(r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
                re.I), "instruction override"),
    (re.compile(r"disregard\s+(all\s+|the\s+|your\s+)?(previous|prior|above|system)?\s*(instructions|rules|guidelines)",
                re.I), "instruction override"),
    (re.compile(r"\byou\s+are\s+now\b", re.I), "role reassignment"),
    (re.compile(r"\b(system|developer)\s+prompt\b", re.I), "system prompt reference"),
    (re.compile(r"</?\s*(system|assistant|user|im_start|im_end)\s*>", re.I), "chat-role markup"),
    (re.compile(r"\bnew\s+instructions?\s*:", re.I), "instruction injection"),
    (re.compile(r"\b(mark|report|declare|treat)\s+(this|it|the\s+code|all\s+checks?)\s+(as\s+)?"
                r"(verified|safe|passing|passed|correct)", re.I), "verdict tampering"),
    (re.compile(r"\b(skip|disable|bypass|turn\s+off)\s+(all\s+)?(the\s+)?(verification|checks?|sandbox|firewall|tests?)",
                re.I), "control bypass"),
    (re.compile(r"\bjailbreak\b|\bDAN\s+mode\b", re.I), "jailbreak"),
]


@dataclass
class Finding:
    rule: str
    severity: str
    line: int
    message: str


@dataclass
class AuditReport:
    allowed: bool
    findings: list[Finding] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "elapsed_ms": round(self.elapsed_ms, 4),
                "findings": [asdict(f) for f in self.findings]}

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in (CRITICAL, HIGH)]


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


class _Walker(ast.NodeVisitor):
    def __init__(self, policy: SandboxPolicy, findings: list[Finding], in_contract: bool = False,
                 bound: frozenset[str] = frozenset()) -> None:
        # lexical scope stack of locally bound names (params/assignments shadow builtins)
        self.scopes: list[frozenset[str]] = [bound]
        self.policy = policy
        self.findings = findings
        self.in_contract = in_contract
        self.allowed_roots = {m.split(".")[0] for m in policy.allowed_imports}
        self.allowed_full = set(policy.allowed_imports)

    def _shadowed(self, name: str) -> bool:
        return any(name in sc for sc in self.scopes)

    def _enter_scope(self, node: ast.AST) -> None:
        self.scopes.append(_local_names(node))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._enter_scope(node)
        self.generic_visit(node)
        self.scopes.pop()

    def add(self, rule: str, severity: str, node: ast.AST, msg: str) -> None:
        line = getattr(node, "lineno", 0)
        self.findings.append(Finding(rule, severity, line, ("contract: " if self.in_contract else "") + msg))

    # -------------------------------------------------------------- imports
    def _check_module(self, name: str, node: ast.AST) -> None:
        if name not in self.allowed_full and name.split(".")[0] not in self.allowed_roots:
            self.add("SF001-import", CRITICAL, node, f"import of non-allowlisted module `{name}`")

    def visit_Import(self, node: ast.Import) -> None:
        if self.in_contract:
            self.add("SF001-import", CRITICAL, node, "import inside contract")
        for a in node.names:
            self._check_module(a.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.add("SF001-import", CRITICAL, node, "relative import")
            return
        self._check_module(node.module or "", node)
        for a in node.names:
            if a.name in DANGEROUS_ATTRS or a.name == "*":
                self.add("SF001-import", HIGH, node, f"wildcard/dangerous name import `{a.name}`")

    # -------------------------------------------------------------- calls
    def visit_Call(self, node: ast.Call) -> None:
        name = _callee_name(node.func)
        if isinstance(node.func, ast.Name) and name in BLOCKED_CALLS and not self._shadowed(name):
            self.add("SF002-dynamic-code", CRITICAL, node, f"call to blocked builtin `{name}()`")
        if isinstance(node.func, ast.Name) and name in REFLECTIVE_GETTERS:
            attr = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
                self.add("SF003-reflection", CRITICAL, node, f"`{name}` with non-literal attribute name")
            elif attr.value.startswith("_") or attr.value in DANGEROUS_ATTRS:
                self.add("SF003-reflection", CRITICAL, node, f"`{name}` reaching private attribute `{attr.value}`")
        if isinstance(node.func, ast.Name) and name == "type" and len(node.args) == 3:
            self.add("SF003-reflection", HIGH, node, "dynamic class creation via type(name, bases, dict)")
        if name in ("format", "format_map") and isinstance(node.func, ast.Attribute):
            fmt = node.func.value
            if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str) and re.search(r"\{[^}]*(\.|\[)\s*_",
                                                                                          fmt.value):
                self.add("SF004-format-traversal", CRITICAL, node, "format-string attribute traversal")
        self._resource_call(node, name)
        self.generic_visit(node)

    def _resource_call(self, node: ast.Call, name: str) -> None:
        if name == "range":
            for a in node.args:
                v = const_eval(a)
                if v is not None and abs(v) > 10**8:
                    self.add("SF006-resource", CRITICAL, node, f"range over {v:.3g} elements")
        if name in ("sleep",) and node.args:
            v = const_eval(node.args[0])
            if v is not None and v > 5:
                self.add("SF006-resource", HIGH, node, f"sleep({v}) exceeds sandbox budget")
        if name in ("setrecursionlimit", "setswitchinterval"):
            self.add("SF006-resource", HIGH, node, f"interpreter tuning call `{name}`")

    # -------------------------------------------------------------- attributes & names
    def visit_Attribute(self, node: ast.Attribute) -> None:
        a = node.attr
        if a in DANGEROUS_ATTRS:
            self.add("SF003-reflection", CRITICAL, node, f"access to dangerous attribute `.{a}`")
        elif a.startswith("__") and a.endswith("__") and a not in SAFE_DUNDER_ATTRS:
            self.add("SF003-reflection", HIGH, node, f"dunder attribute access `.{a}`")
        elif self.in_contract and a.startswith("_"):
            self.add("SF003-reflection", HIGH, node, f"private attribute `.{a}` in contract")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in DANGEROUS_NAMES:
            self.add("SF003-reflection", CRITICAL, node, f"reference to `{node.id}`")
        if node.id in BLOCKED_CALLS and isinstance(node.ctx, ast.Load) and not self._shadowed(node.id):
            # aliasing a blocked builtin (f = eval) is as bad as calling it
            self.add("SF002-dynamic-code", CRITICAL, node, f"reference to blocked builtin `{node.id}`")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        s = node.slice
        if isinstance(s, ast.Constant) and isinstance(s.value, str) and s.value.startswith("__"):
            self.add("SF003-reflection", CRITICAL, node, f"subscript with dunder key `{s.value}`")
        self.generic_visit(node)

    # -------------------------------------------------------------- resource exhaustion
    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Pow):
            exp = const_eval(node.right)
            if isinstance(node.right, ast.BinOp) and isinstance(node.right.op, ast.Pow):
                self.add("SF006-resource", CRITICAL, node, "power tower (exponential blow-up)")
            elif exp is not None and exp > 10_000:
                self.add("SF006-resource", CRITICAL, node, f"huge exponent {exp:.3g}")
        if isinstance(node.op, ast.Mult):
            for side, other in ((node.left, node.right), (node.right, node.left)):
                n = const_eval(other)
                is_seq = isinstance(side, (ast.List, ast.Tuple, ast.Constant)) and not (
                    isinstance(side, ast.Constant) and isinstance(side.value, (int, float)))
                if is_seq and n is not None and n > 10**7:
                    self.add("SF006-resource", CRITICAL, node, f"sequence repetition x{n:.3g}")
        if isinstance(node.op, ast.LShift):
            n = const_eval(node.right)
            if n is not None and n > 100_000:
                self.add("SF006-resource", CRITICAL, node, f"shift by {n:.3g} bits")
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        test = node.test
        truthy = isinstance(test, ast.Constant) and bool(test.value)
        if truthy:
            exits = any(isinstance(n, (ast.Break, ast.Return, ast.Raise)) for n in ast.walk(node)
                        if n is not node)
            if not exits:
                self.add("SF006-resource", CRITICAL, node, "unconditional infinite loop (no break/return/raise)")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for d in node.decorator_list:
            if _callee_name(d.func if isinstance(d, ast.Call) else d) in BLOCKED_CALLS:
                self.add("SF002-dynamic-code", CRITICAL, d, "blocked builtin used as decorator")
        self._enter_scope(node)
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Global(self, node: ast.Global) -> None:
        for n in node.names:
            if n.startswith("__"):
                self.add("SF003-reflection", CRITICAL, node, f"global rebinding of `{n}`")


def _scan_text(text: str, line: int, where: str, findings: list[Finding]) -> None:
    for pat, label in INJECTION_PATTERNS:
        if pat.search(text):
            findings.append(Finding("SF007-prompt-injection", CRITICAL, line,
                                    f"{label} phrase in {where}: {pat.search(text).group(0)!r}"))  # type: ignore[union-attr]


def _scan_strings_and_comments(source: str, tree: ast.AST, findings: list[Finding]) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 8:
            _scan_text(node.value, getattr(node, "lineno", 0), "string literal/docstring", findings)
            if re.search(r"\{[^}]*\.\s*__\w+__", node.value):
                findings.append(Finding("SF004-format-traversal", CRITICAL, getattr(node, "lineno", 0),
                                        "format-string attribute traversal template"))
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                _scan_text(tok.string, tok.start[0], "comment", findings)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass


def _check_contracts(tree: ast.Module, policy: SandboxPolicy, findings: list[Finding]) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node)
            if not doc:
                continue
            clauses = parse_contract(doc)
            line = getattr(node, "lineno", 1)
            for bad in clauses.parse_errors:
                findings.append(Finding("SF008-contract", MEDIUM, line, f"unparseable contract clause {bad!r}"))
            for kind, expr in clauses.all_expressions():
                walker = _Walker(policy, findings, in_contract=True)
                try:
                    expr_tree = ast.parse(expr, mode="eval")
                except SyntaxError:
                    continue
                for sub in ast.walk(expr_tree):
                    if isinstance(sub, ast.Lambda):
                        findings.append(Finding("SF008-contract", HIGH, line, f"lambda inside :{kind}: clause"))
                    if isinstance(sub, ast.NamedExpr):
                        findings.append(Finding("SF008-contract", HIGH, line, f"assignment inside :{kind}: clause"))
                walker.visit(expr_tree)


def _local_names(scope: ast.AST) -> frozenset[str]:
    """Names bound directly in ``scope`` (not in nested function scopes)."""
    bound: set[str] = set()
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = scope.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None:
                bound.add(arg.arg)
        body = scope.body if isinstance(scope.body, list) else [scope.body]
    else:
        body = getattr(scope, "body", [])
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            continue  # nested scope: do not descend
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            bound.add(n.id)
        stack.extend(ast.iter_child_nodes(n))
    return frozenset(bound)


def audit_source(source: str, policy: Optional[SandboxPolicy] = None) -> AuditReport:
    policy = policy or SandboxPolicy()
    t0 = time.perf_counter()
    findings: list[Finding] = []
    if len(source.encode("utf-8", "replace")) > policy.max_source_bytes:
        findings.append(Finding("SF009-size", CRITICAL, 0, f"source exceeds {policy.max_source_bytes} bytes"))
        return AuditReport(False, findings, (time.perf_counter() - t0) * 1000)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        findings.append(Finding("SF000-syntax", CRITICAL, exc.lineno or 0, f"syntax error: {exc.msg}"))
        return AuditReport(False, findings, (time.perf_counter() - t0) * 1000)
    n_nodes = sum(1 for _ in ast.walk(tree))
    if n_nodes > policy.max_ast_nodes:
        findings.append(Finding("SF009-size", CRITICAL, 0, f"{n_nodes} AST nodes exceeds limit"))
    _Walker(policy, findings, bound=_local_names(tree)).visit(tree)
    _scan_strings_and_comments(source, tree, findings)
    _check_contracts(tree, policy, findings)
    allowed = not any(f.severity in (CRITICAL, HIGH) for f in findings)
    return AuditReport(allowed, findings, (time.perf_counter() - t0) * 1000)


def audit_expression(expr: str, policy: Optional[SandboxPolicy] = None) -> AuditReport:
    """Audit a single expression (used for LLM-proposed contracts)."""
    policy = policy or SandboxPolicy()
    t0 = time.perf_counter()
    findings: list[Finding] = []
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        return AuditReport(False, [Finding("SF000-syntax", CRITICAL, 0, str(exc))], 0.0)
    _Walker(policy, findings, in_contract=True).visit(tree)
    allowed = not any(f.severity in (CRITICAL, HIGH) for f in findings)
    return AuditReport(allowed, findings, (time.perf_counter() - t0) * 1000)
