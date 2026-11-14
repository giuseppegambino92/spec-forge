"""Builder agent: spectrum fault localisation + MCTS search over the repair grammar.

Pipeline for one repair request
  1. *Trace* failing and passing inputs inside the sandbox with ``sys.settrace`` (line spectra
     + variable-state snapshots of the minimal counterexample).
  2. *Localise* with the Ochiai coefficient  susp(l) = ef / sqrt(F * (ef + ep)); lines where an
     exception was raised get susp = 1.
  3. *Search* a tree of patched programs with PUCT-guided MCTS. Actions are grammar edits whose
     priors combine (violation kind x edit category) affinity with line suspiciousness;
     progressive widening keeps the branching factor proportional to sqrt(visits).
     A node's value is a *verifier* score (no random rollouts): the fraction of every
     historical counterexample (Regression Monotonicity) the candidate passes plus the
     fraction of properties surviving a fixed-seed adversarial campaign.
  4. The first candidate that passes all historical counterexamples and the quick campaign is
     returned with its unified diff; the CEGIS loop then re-attacks it with a fresh budget.
"""

from __future__ import annotations

import difflib
import hashlib
import math
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import FuzzConfig, MCTSConfig, ShrinkConfig
from ..codec import safe_repr
from ..sandbox.firewall import audit_source
from ..symbolic.ast_analyzer import analyze_module
from ..symbolic.invariants import CheckContext, Property, TargetSpec, check_case
from .repair_grammar import Candidate, local_candidates, parse_ok, template_candidates

TARGET_FILENAME = "<specforge-target>"

# violation kind -> edit category -> affinity (how plausible the category fixes that kind of failure)
AFFINITY: dict[str, dict[str, float]] = {
    "totality:ZeroDivisionError": {"zero_div_guard": 1.0, "binop_swap": 0.2, "arith_nudge": 0.2, "compare_swap": 0.2},
    "totality:IndexError": {"index_clamp": 1.0, "const_perturb": 0.45, "arith_nudge": 0.45, "compare_swap": 0.4},
    "totality:TimeoutError": {"compare_swap": 0.7, "arith_nudge": 0.5, "const_perturb": 0.4},
    "totality": {"compare_swap": 0.4, "const_perturb": 0.35, "arith_nudge": 0.35, "zero_div_guard": 0.3,
                 "index_clamp": 0.3, "defensive_copy": 0.2},
    "postcondition:bounds": {"contract_clamp": 1.0, "saturating_sub": 0.95, "compare_swap": 0.4,
                             "const_perturb": 0.3, "arith_nudge": 0.3},
    "postcondition:conservation": {"conservation_rebalance": 1.0, "exact_sum": 0.5, "arith_nudge": 0.3,
                                   "const_perturb": 0.3},
    "postcondition": {"compare_swap": 0.65, "const_perturb": 0.5, "arith_nudge": 0.5, "contract_clamp": 0.4,
                      "binop_swap": 0.3, "saturating_sub": 0.3, "zero_div_guard": 0.3},
    "law": {"const_perturb": 0.7, "compare_swap": 0.6, "arith_nudge": 0.6, "binop_swap": 0.3},
    "idempotency": {"fixpoint_closure": 1.0, "compare_swap": 0.3, "const_perturb": 0.2},
    "roundtrip": {"arith_nudge": 0.7, "const_perturb": 0.7, "compare_swap": 0.6, "binop_swap": 0.3},
    "permutation_invariance": {"exact_sum": 1.0, "arith_nudge": 0.1},
    "no_input_mutation": {"defensive_copy": 1.0, "mutable_default": 0.3},
    "determinism": {"mutable_default": 1.0, "defensive_copy": 0.8},
    "finite_output": {"zero_div_guard": 0.9, "contract_clamp": 0.3, "exact_sum": 0.3},
    "atomicity": {"lock_isolation": 1.0},
}
DEFAULT_AFFINITY = 0.08


def violation_kind(prop_kind: str, message: str, expr: str = "") -> str:
    if prop_kind == "totality":
        exc = message.split(":")[0].replace("raised ", "").strip()
        key = f"totality:{exc}"
        return key if key in AFFINITY else "totality"
    if prop_kind == "postcondition":
        if "sum(result)" in expr:
            return "postcondition:conservation"
        if "result" in expr and any(op in expr for op in ("<=", ">=", "<", ">")) and "for " not in expr:
            return "postcondition:bounds"
        return "postcondition"
    return prop_kind


# --------------------------------------------------------------------------- worker-side jobs


def evaluate_candidate(ctx: CheckContext, props: list[Property], fuzz: FuzzConfig, cases: list[dict[str, Any]],
                       quick_trials: int, seed: int) -> dict[str, Any]:
    from .breaker_agent import run_campaign

    cex_fail: list[dict[str, Any]] = []
    for c in cases:
        if ctx.spec.kind == "function" and not ctx.precondition_holds(c):
            continue
        fails = check_case(ctx, props, c)
        if fails:
            cex_fail.append({"case": c, "property": fails[0][0], "message": fails[0][1]})
    out: dict[str, Any] = {"cex_total": len(cases), "cex_failed": len(cex_fail), "props_total": len(props),
                           "props_failed": [], "first_failure": cex_fail[0] if cex_fail else None}
    if cex_fail:
        out["props_failed"] = sorted({f["property"] for f in cex_fail})
        return out
    if ctx.spec.kind == "class":
        quick_trials = min(quick_trials, 48)  # thread-harness trials are ~100x costlier than calls
    res = run_campaign(ctx, props, fuzz, ShrinkConfig(), trials=quick_trials, seed=seed, shrink=False,
                       stop_early=True)
    out["props_failed"] = sorted(res.property_failures)
    out["quick_trials"] = res.trials
    if res.failing_cases:
        fc = res.failing_cases[0]
        fails = check_case(ctx, props, fc)
        out["first_failure"] = {"case": fc, "property": fails[0][0] if fails else "?",
                                "message": fails[0][1] if fails else "?"}
    return out


def trace_cases(ctx: CheckContext, props: list[Property], failing: list[dict[str, Any]],
                passing: list[dict[str, Any]]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    exception_lines: list[int] = []
    state_trace: list[dict[str, Any]] = []
    for idx, case in enumerate(failing + passing):
        lines: set[int] = set()
        capture = idx == 0
        exc_line = {"line": None}

        def local(frame: Any, event: str, arg: Any) -> Any:
            if event == "line":
                lines.add(frame.f_lineno)
                if capture and len(state_trace) < 80:  # noqa: B023
                    state_trace.append({"line": frame.f_lineno, "fn": frame.f_code.co_name,
                                        "locals": {k: safe_repr(v, 60) for k, v in frame.f_locals.items()
                                                   if not k.startswith("__")}})
            elif event == "exception":
                exc_line["line"] = frame.f_lineno  # noqa: B023
            return local

        def glob(frame: Any, event: str, arg: Any) -> Any:
            return local if frame.f_code.co_filename == TARGET_FILENAME else None

        old = sys.gettrace()
        sys.settrace(glob)
        threading.settrace(glob)
        try:
            fails = check_case(ctx, props, case)
        finally:
            sys.settrace(old)
            threading.settrace(None)  # type: ignore[arg-type]
        runs.append({"failed": bool(fails), "lines": sorted(lines)})
        if fails and exc_line["line"] is not None and fails[0][1].startswith("raised"):
            exception_lines.append(exc_line["line"])
    return {"runs": runs, "exception_lines": sorted(set(exception_lines)), "state_trace": state_trace}


# --------------------------------------------------------------------------- localisation


def ochiai(runs: list[dict[str, Any]], exception_lines: list[int]) -> dict[int, float]:
    total_failed = sum(1 for r in runs if r["failed"])
    ef: dict[int, int] = {}
    ep: dict[int, int] = {}
    for r in runs:
        for ln in r["lines"]:
            (ef if r["failed"] else ep)[ln] = (ef if r["failed"] else ep).get(ln, 0) + 1
    scores: dict[int, float] = {}
    for ln in set(ef) | set(ep):
        f, p = ef.get(ln, 0), ep.get(ln, 0)
        denom = math.sqrt(total_failed * (f + p)) if total_failed and (f + p) else 0.0
        scores[ln] = f / denom if denom else 0.0
    for ln in exception_lines:
        scores[ln] = 1.0
    return scores


def map_lines(old: str, new: str) -> dict[int, int]:
    """new line number -> old line number for unchanged regions."""
    sm = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines(), autojunk=False)
    out: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out[j1 + k + 1] = i1 + k + 1
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                out[j1 + k + 1] = i1 + k + 1
    return out


def unified_diff(old: str, new: str, name: str = "target.py") -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"a/{name}", tofile=f"b/{name}"))


def src_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- MCTS


@dataclass
class MCTSNode:
    source: str
    edits: list[str]
    categories: list[str]
    depth: int
    prior: float = 1.0
    parent: Optional["MCTSNode"] = None
    children: list["MCTSNode"] = field(default_factory=list)
    untried: Optional[list[Candidate]] = None
    visits: int = 0
    value_sum: float = 0.0
    reward: float = 0.0
    success: bool = False
    evaluation: dict[str, Any] = field(default_factory=dict)

    @property
    def q(self) -> float:
        return self.value_sum / self.visits if self.visits else 0.0


@dataclass
class RepairResult:
    success: bool
    patched_source: Optional[str]
    edits: list[str]
    categories: list[str]
    diff: str
    evaluations: int
    iterations: int
    best_reward: float
    elapsed_ms: float
    suspicious_lines: list[tuple[int, float]]
    state_trace: list[dict[str, Any]]
    tree_size: int
    rejected_by_firewall: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "state_trace"} | {
            "state_trace": self.state_trace[:40]}


class BuilderAgent:
    def __init__(self, runner: Any, mcts: MCTSConfig, fuzz: FuzzConfig, policy: Any = None,
                 on_event: Optional[Callable[[str, dict[str, Any]], None]] = None) -> None:
        self.runner = runner
        self.cfg = mcts
        self.fuzz = fuzz
        self.policy = policy
        self.on_event = on_event or (lambda kind, payload: None)

    # ------------------------------------------------------------------ helpers
    def _job(self, op: str, source: str, spec: TargetSpec, **extra: Any) -> dict[str, Any]:
        return self.runner.submit({"op": op, "source": source, "spec": spec.to_dict(), "fuzz": self.fuzz.__dict__,
                                   **extra})

    def localize(self, source: str, spec: TargetSpec, failing: list[dict[str, Any]],
                 passing: list[dict[str, Any]]) -> tuple[dict[int, float], list[dict[str, Any]]]:
        res = self._job("trace", source, spec, failing=failing[:6], passing=passing[:24])
        if not res.get("ok"):
            return {}, []
        return ochiai(res["runs"], res["exception_lines"]), res.get("state_trace", [])

    def _scopes(self, source: str, spec: TargetSpec) -> list[tuple[int, int]]:
        facts = analyze_module(source)
        scopes: list[tuple[int, int]] = []
        if spec.kind == "class":
            cls = facts.klass(spec.name)
            if cls:
                scopes.append((cls.lineno, cls.end_lineno))
            return scopes
        fn = facts.function(spec.name)
        if fn:
            scopes.append((fn.lineno, fn.end_lineno))
        for p in spec.properties:
            dec = p.params.get("decoder")
            if dec and facts.function(dec):
                d = facts.function(dec)
                scopes.append((d.lineno, d.end_lineno))  # type: ignore[union-attr]
        # helpers called by the target are in scope too
        if fn:
            for f in facts.functions:
                if f.name in fn.calls and f.name != fn.name:
                    scopes.append((f.lineno, f.end_lineno))
        return scopes

    def _actions(self, node: MCTSNode, root_src: str, spec: TargetSpec, susp: dict[int, float],
                 kinds: dict[str, float]) -> list[Candidate]:
        src = node.source
        line_map = map_lines(root_src, src)
        scopes = self._scopes(src, spec)
        ensures = [p.params["expr"] for p in spec.properties if p.kind == "postcondition"]
        rmw: list[str] = []
        for p in spec.properties:
            rmw.extend(p.params.get("attrs", []))
        cands: list[Candidate] = []
        try:
            cands.extend(local_candidates(src, scopes, skip_lines=set()))
            cands.extend(template_candidates(src, spec.name, spec.name if spec.kind == "class" else None,
                                             spec.method, ensures, rmw))
        except SyntaxError:
            return []
        if node.depth == 0 and self.policy is not None:
            for i, proposal in enumerate(self._llm_proposals(src, spec, kinds)):
                cands.append(Candidate("llm_rewrite", f"LLM policy proposal #{i + 1}", 0, proposal, local=False))
        max_s = max(susp.values()) if susp else 0.0
        for c in cands:
            aff = max((AFFINITY.get(k, {}).get(c.category, DEFAULT_AFFINITY) * w for k, w in kinds.items()),
                      default=DEFAULT_AFFINITY)
            if c.category == "llm_rewrite":
                aff = 0.9
            if c.local:
                orig = line_map.get(c.line, c.line)
                s = susp.get(orig, 0.0) / max_s if max_s else 0.0
                c.prior = aff * (0.25 + 0.75 * s)
            else:
                c.prior = aff
            if not self.cfg.use_priors:
                c.prior = 1.0  # ablation: uninformed search
            # never repeat a category that is already applied on this path
            if c.category in node.categories and not c.local:
                c.prior *= 0.05
        seen: set[str] = set()
        uniq: list[Candidate] = []
        for c in sorted(cands, key=lambda c: (-c.prior, c.category, c.line, c.description)):
            h = src_hash(c.source)
            if h in seen or c.source == src:
                continue
            seen.add(h)
            uniq.append(c)
        return uniq

    def _llm_proposals(self, src: str, spec: TargetSpec, kinds: dict[str, float]) -> list[str]:
        try:
            return list(self.policy.propose(source=src, target=spec.name, violations=list(kinds),
                                            properties=[p.description for p in spec.properties]))
        except Exception as exc:  # noqa: BLE001 - LLM failures degrade to the symbolic grammar
            self.on_event("builder.llm_error", {"error": str(exc)})
            return []

    def _evaluate(self, node: MCTSNode, spec: TargetSpec, cases: list[dict[str, Any]], cache: dict[str, Any],
                  refuted: set[str], stats: dict[str, int]) -> None:
        h = src_hash(node.source)
        if h in cache:
            ev = cache[h]
        elif h in refuted:
            ev = {"refuted": True}
        elif not parse_ok(node.source):
            ev = {"invalid": True}
        else:
            audit = audit_source(node.source)
            if not audit.allowed:
                stats["firewall"] += 1
                ev = {"firewall": [f.message for f in audit.blocking][:3]}
            else:
                stats["evaluations"] += 1
                ev = self._job("evaluate", node.source, spec, cases=cases, quick_trials=self.cfg.quick_trials,
                               seed=self.cfg.seed + 101)
            cache[h] = ev
        node.evaluation = ev
        if not ev.get("ok"):
            node.reward, node.success = 0.0, False
            return
        cex_frac = 1.0 - ev["cex_failed"] / ev["cex_total"] if ev["cex_total"] else 1.0
        prop_frac = 1.0 - len(ev["props_failed"]) / max(1, ev["props_total"])
        node.success = ev["cex_failed"] == 0 and not ev["props_failed"]
        node.reward = 0.6 * cex_frac + 0.4 * prop_frac - 0.03 * node.depth + (0.5 if node.success else 0.0)

    def _select(self, node: MCTSNode) -> MCTSNode:
        c = self.cfg.c_puct
        sqrt_n = math.sqrt(node.visits + 1)
        return max(node.children, key=lambda ch: ch.q + c * ch.prior * sqrt_n / (1 + ch.visits))

    def _can_widen(self, node: MCTSNode) -> bool:
        return bool(node.untried) and len(node.children) < max(2, int(2.0 * math.sqrt(node.visits + 1)))

    # ------------------------------------------------------------------ main entry
    def repair(self, source: str, spec: TargetSpec, counterexamples: list[dict[str, Any]],
               passing: list[dict[str, Any]], violated: list[tuple[str, str]],
               refuted: Optional[set[str]] = None) -> RepairResult:
        """``violated``: [(property name, message)] observed by the Breaker for this target."""
        t0 = time.perf_counter()
        refuted = refuted or set()
        by_name = {p.name: p for p in spec.properties}
        kinds: dict[str, float] = {}
        for name, msg in violated:
            p = by_name.get(name)
            if p is None:
                continue
            k = violation_kind(p.kind, msg, p.params.get("expr", ""))
            kinds[k] = max(kinds.get(k, 0.0), 1.0 if not kinds else 0.8)
        susp, state_trace = self.localize(source, spec, counterexamples, passing)
        if not self.cfg.use_priors:
            susp = {}
        top = sorted(susp.items(), key=lambda kv: -kv[1])[: self.cfg.top_suspicious_lines]
        self.on_event("builder.localized", {"suspicious": top, "kinds": sorted(kinds)})

        root = MCTSNode(source=source, edits=[], categories=[], depth=0)
        cache: dict[str, Any] = {}
        stats = {"evaluations": 0, "firewall": 0}
        rng = random.Random(self.cfg.seed)
        best: Optional[MCTSNode] = None
        found: Optional[MCTSNode] = None
        log: list[dict[str, Any]] = []
        iterations = 0
        tree_size = 1
        while stats["evaluations"] < self.cfg.iterations and iterations < self.cfg.iterations * 4:
            iterations += 1
            node = root
            path = [root]
            # selection
            while True:
                if node.untried is None and node.depth < self.cfg.max_depth:
                    node.untried = self._actions(node, source, spec, susp, kinds)
                if node.depth < self.cfg.max_depth and self._can_widen(node):
                    break
                if not node.children:
                    break
                node = self._select(node)
                path.append(node)
            # expansion
            if node.depth < self.cfg.max_depth and node.untried:
                cand = node.untried.pop(0)
                child = MCTSNode(source=cand.source, edits=node.edits + [cand.description],
                                 categories=node.categories + [cand.category], depth=node.depth + 1,
                                 prior=cand.prior, parent=node)
                node.children.append(child)
                tree_size += 1
                self._evaluate(child, spec, counterexamples, cache, refuted, stats)
                path.append(child)
                leaf = child
                log.append({"edits": child.edits, "reward": round(child.reward, 4), "success": child.success,
                            "eval": {k: v for k, v in child.evaluation.items() if k in ("cex_failed", "cex_total",
                                                                                       "props_failed")}})
                self.on_event("builder.candidate", log[-1])
            else:
                leaf = node
                if not leaf.evaluation:
                    self._evaluate(leaf, spec, counterexamples, cache, refuted, stats)
                if not node.untried and not node.children and node is root:
                    break  # no actions at all
            # backpropagation
            for n in path:
                n.visits += 1
                n.value_sum += leaf.reward
            if best is None or leaf.reward > best.reward:
                best = leaf
            if leaf.success:
                found = leaf
                break
            if all(not n.untried for n in path) and not any(self._expandable(n) for n in self._iter(root)):
                break
            _ = rng  # deterministic: PUCT ties are broken by stable ordering, rng reserved for extensions
        chosen = found
        elapsed = (time.perf_counter() - t0) * 1000
        return RepairResult(
            success=found is not None,
            patched_source=chosen.source if chosen else None,
            edits=chosen.edits if chosen else (best.edits if best else []),
            categories=chosen.categories if chosen else [],
            diff=unified_diff(source, chosen.source) if chosen else "",
            evaluations=stats["evaluations"], iterations=iterations,
            best_reward=round(best.reward if best else 0.0, 4), elapsed_ms=round(elapsed, 2),
            suspicious_lines=[(ln, round(s, 3)) for ln, s in top], state_trace=state_trace, tree_size=tree_size,
            rejected_by_firewall=stats["firewall"], log=log,
        )

    def _iter(self, node: MCTSNode):
        yield node
        for c in node.children:
            yield from self._iter(c)

    def _expandable(self, node: MCTSNode) -> bool:
        return node.depth < self.cfg.max_depth and (node.untried is None or bool(node.untried))
