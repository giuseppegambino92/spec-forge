"""Spec Architect agent: symbolic invariant extraction.

Turns a module's AST facts + docstring contracts into ``TargetSpec`` objects: preconditions
(compiled into an interval constraint set for directed generation) and a list of formal
properties, each tagged with its *origin* (docstring vs inferred) and a confidence score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..symbolic.ast_analyzer import ClassFacts, FunctionFacts, ModuleFacts, analyze_module
from ..symbolic.constraints import solve_preconditions
from ..symbolic.invariants import PropertySpec, TargetSpec

IDEMPOTENT_RE = re.compile(r"(normali[sz]e|canonical|sanitize|clean|dedup|slugify|strip|trim|collapse|unique|"
                           r"squash|tidy|compact)", re.I)
AGGREGATE_RE = re.compile(r"(total|sum|aggregate|count|mean|average|balance|net_|checksum|tally)", re.I)
INPLACE_RE = re.compile(r"(in_?place|_inplace|^mutate|^apply_to)", re.I)
INVERSE_PAIRS = [("encode", "decode"), ("serialize", "deserialize"), ("dumps", "loads"), ("compress", "decompress"),
                 ("encrypt", "decrypt"), ("pack", "unpack"), ("escape", "unescape"), ("quote", "unquote"),
                 ("to_", "from_"), ("marshal", "unmarshal"), ("format", "parse")]
MUTABLE_KINDS = {"list", "dict", "set"}


@dataclass
class ModuleSpec:
    targets: list[TargetSpec] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"targets": [t.to_dict() for t in self.targets], "skipped": self.skipped}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModuleSpec":
        return cls([TargetSpec.from_dict(t) for t in d["targets"]], d.get("skipped", []))

    def target(self, name: str) -> Optional[TargetSpec]:
        return next((t for t in self.targets if t.name == name), None)


def _find_inverse(fn: FunctionFacts, facts: ModuleFacts) -> Optional[str]:
    explicit = fn.clauses.flags.get("inverse")
    if explicit:
        return explicit.strip()
    names = {f.name for f in facts.functions}
    for enc, dec in INVERSE_PAIRS:
        if enc in fn.name:
            cand = fn.name.replace(enc, dec, 1)
            if cand != fn.name and cand in names:
                return cand
    return None


class SpecAgent:
    """Extracts formal contracts from source and synthesises property checks (as data)."""

    def extract(self, source: str, targets: Optional[list[str]] = None) -> ModuleSpec:
        facts = analyze_module(source)
        out = ModuleSpec()
        decoders = set()
        for fn in facts.functions:
            inv = _find_inverse(fn, facts)
            if inv:
                decoders.add(inv)
        for fn in facts.functions:
            if not fn.public or (targets and fn.name not in targets):
                continue
            out.targets.append(self._function_spec(fn, facts))
        for cls in facts.classes:
            if not cls.public or (targets and cls.name not in targets):
                continue
            specs = self._class_specs(cls)
            if specs:
                out.targets.extend(specs)
            else:
                out.skipped.append(f"class {cls.name}: no concurrency/atomicity obligations inferred")
        return out

    # ------------------------------------------------------------------ functions
    def _function_spec(self, fn: FunctionFacts, facts: ModuleFacts) -> TargetSpec:
        c = fn.clauses
        props: list[PropertySpec] = []
        notes: list[str] = []
        allowed = sorted(set(c.raises) | set(fn.explicit_raises))
        props.append(PropertySpec("totality", "totality",
                                  "terminates and raises only documented exceptions"
                                  + (f" ({', '.join(allowed)})" if allowed else ""), "inferred", 0.9))
        for i, e in enumerate(c.ensures):
            props.append(PropertySpec("postcondition", f"ensures[{i}]", e, "docstring", 1.0, {"expr": e}))
        for i, e in enumerate(c.laws):
            props.append(PropertySpec("law", f"law[{i}]", e, "docstring", 1.0, {"expr": e}))

        required = [p for p in fn.params if not p.has_default and p.kind == "positional"]
        first = fn.params[0] if fn.params else None

        if fn.returns_value and first is not None and (
                "idempotent" in c.flags or (IDEMPOTENT_RE.search(fn.name) and (
                    fn.returns.kind in ("any", first.type.kind)))):
            origin = "docstring" if "idempotent" in c.flags else "inferred"
            props.append(PropertySpec("idempotency", "idempotency", f"{fn.name}({fn.name}(x)) == {fn.name}(x)",
                                      origin, 1.0 if origin == "docstring" else 0.8))
            notes.append(f"idempotency inferred from name `{fn.name}`" if origin == "inferred" else
                         "idempotency declared")

        inverse = _find_inverse(fn, facts)
        if inverse and fn.returns_value and len(required) == 1:
            props.append(PropertySpec("roundtrip", f"roundtrip[{inverse}]", f"{inverse}({fn.name}(x)) == x",
                                      "docstring" if "inverse" in c.flags else "inferred", 0.9,
                                      {"decoder": inverse, "param": required[0].name}))
            notes.append(f"round-trip pair inferred: {fn.name} <-> {inverse}")

        list_params = [p for p in fn.params if p.type.kind == "list"]
        if fn.returns_value and len(list_params) == 1 and fn.returns.kind in ("int", "float") and (
                "order_independent" in c.flags or AGGREGATE_RE.search(fn.name)):
            props.append(PropertySpec("permutation_invariance", "permutation_invariance",
                                      f"{fn.name} is invariant under permutation of `{list_params[0].name}`",
                                      "docstring" if "order_independent" in c.flags else "inferred", 0.85,
                                      {"param": list_params[0].name}))
            notes.append(f"aggregate over `{list_params[0].name}` -> order independence")

        mutable = [p.name for p in fn.params if p.type.kind in MUTABLE_KINDS]
        if fn.returns_value and mutable and "mutates" not in c.flags and not INPLACE_RE.search(fn.name):
            statically = [p for p in mutable if p in fn.mutated_params]
            props.append(PropertySpec("no_input_mutation", "no_input_mutation",
                                      f"arguments {', '.join(mutable)} are not mutated", "inferred",
                                      0.9 if statically else 0.6, {"params": mutable}))
            if statically:
                notes.append(f"static dataflow: in-place mutation of {', '.join(statically)}")

        if fn.returns_value and not fn.nondeterministic and not fn.writes_globals:
            mutable_defaults = [p.name for p in fn.params if p.default_is_mutable]
            props.append(PropertySpec("determinism", "determinism", "equal inputs -> equal outputs (no hidden state)",
                                      "inferred", 1.0 if mutable_defaults else 0.85))
            if mutable_defaults:
                notes.append(f"mutable default argument(s): {', '.join(mutable_defaults)}")

        if fn.returns.kind == "float" or (fn.returns.kind == "list" and fn.returns.args
                                          and fn.returns.args[0].kind == "float"):
            props.append(PropertySpec("finite_output", "finite_output", "finite inputs -> finite outputs",
                                      "inferred", 0.8))

        for r in fn.risk_sites:
            if r.kind == "division":
                notes.append(f"line {r.line}: division `{r.detail}` (zero-divisor hazard)")
            elif r.kind == "index":
                notes.append(f"line {r.line}: computed index `{r.detail}` (bounds hazard)")
        cs = solve_preconditions(c.requires)
        return TargetSpec(
            name=fn.name, kind="function", params=[p.to_dict() for p in fn.params], requires=list(c.requires),
            allowed_raises=allowed, properties=props, constraints=cs.to_dict(), lineno=fn.lineno,
            end_lineno=fn.end_lineno, notes=notes,
        )

    # ------------------------------------------------------------------ classes
    def _class_specs(self, cls: ClassFacts) -> list[TargetSpec]:
        declared = cls.clauses.flags.get("thread_safe")
        wanted = {m.strip() for m in declared.split(",")} if declared else set()
        specs: list[TargetSpec] = []
        for m in cls.methods:
            if not m.public or m.name == "__init__":
                continue
            rmw = m.rmw_attrs
            if not ((m.name in wanted) or (cls.mentions_concurrency and rmw)):
                continue
            attrs = rmw or [a for a in m.self_writes]
            if not attrs:
                continue
            specs.append(TargetSpec(
                name=cls.name, kind="class", params=[p.to_dict() for p in m.params],
                properties=[PropertySpec("atomicity", f"atomicity[{m.name}]",
                                         f"concurrent {m.name}() calls linearise (attrs {', '.join(attrs)})",
                                         "docstring" if m.name in wanted else "inferred",
                                         1.0 if m.name in wanted else 0.75,
                                         {"method": m.name, "attrs": attrs})],
                lineno=m.lineno, end_lineno=m.end_lineno, method=m.name,
                ctor_params=[p.to_dict() for p in cls.init_params],
                notes=[f"read-modify-write on self.{a} in {m.name}()" for a in rmw],
            ))
        return specs
