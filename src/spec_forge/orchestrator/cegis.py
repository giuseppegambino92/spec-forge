"""The CEGIS loop wired as a cyclic state graph.

    firewall --> spec --> select --+--> break --(no counterexample)--> select
                    ^              |      |
                    |              |   (counterexample, heal mode)
                    |              |      v
                    +--------------+--- build --(candidate)--> break   (Breaker re-attacks the candidate)
                                   |
                                   +--> END (queue empty)

Counterexample-Guided Inductive Synthesis: the Builder synthesises a candidate consistent with
*every* counterexample in the ledger; the Breaker searches for a new counterexample against
that candidate with a fresh seed; refuted candidates are blacklisted in the ledger.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..agents.breaker_agent import BreakerAgent
from ..agents.builder_agent import BuilderAgent, src_hash, unified_diff
from ..agents.spec_agent import ModuleSpec, SpecAgent
from ..config import SpecForgeConfig
from ..sandbox.firewall import AuditReport, audit_source
from ..sandbox.runner import SandboxRunner
from ..symbolic.invariants import TargetSpec
from .memory import CounterexampleLedger
from .state_graph import END, EventBus, StateGraph


def target_key(t: TargetSpec) -> str:
    return f"{t.name}.{t.method}" if t.kind == "class" and t.method else t.name


@dataclass
class TargetOutcome:
    target: str
    status: str = "pending"            # verified | falsified | repaired | unrepaired | error
    properties: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    counterexamples: list[dict[str, Any]] = field(default_factory=list)
    first_campaign: dict[str, Any] = field(default_factory=dict)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class CegisState:
    source_original: str
    source_current: str
    mode: str                                   # verify | heal
    session: str
    requested_targets: Optional[list[str]] = None
    audit: Optional[AuditReport] = None
    spec: Optional[ModuleSpec] = None
    queue: list[TargetSpec] = field(default_factory=list)
    current: Optional[TargetSpec] = None
    round: int = 0
    pending_candidate: Optional[str] = None
    pending_repair: Optional[dict[str, Any]] = None
    last_campaign: dict[str, Any] = field(default_factory=dict)
    outcomes: list[TargetOutcome] = field(default_factory=list)
    status: str = "running"

    @property
    def outcome(self) -> TargetOutcome:
        return self.outcomes[-1]


class SpecForge:
    """Facade: audit / verify / heal a Python module."""

    def __init__(self, config: Optional[SpecForgeConfig] = None, runner: Optional[SandboxRunner] = None,
                 ledger: Optional[CounterexampleLedger] = None, policy: Any = None,
                 bus: Optional[EventBus] = None) -> None:
        self.config = config or SpecForgeConfig()
        self.bus = bus or EventBus()
        self.runner = runner or SandboxRunner(self.config.sandbox)
        self._own_runner = runner is None
        if ledger is None:
            path = self.config.ledger_path
            if path != ":memory:":
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                except OSError:
                    path = os.path.join(tempfile.gettempdir(), "specforge-ledger.db")
            ledger = CounterexampleLedger(path)
        self.ledger = ledger
        self.spec_agent = SpecAgent()
        self.breaker = BreakerAgent(self.runner, self.config.fuzz, self.config.shrink)
        self.builder = BuilderAgent(self.runner, self.config.mcts, self.config.fuzz, policy=policy,
                                    on_event=lambda k, p: self.bus.publish(k, p))

    def close(self) -> None:
        if self._own_runner:
            self.runner.close()

    def __enter__(self) -> "SpecForge":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ public API
    def audit(self, source: str) -> AuditReport:
        rep = audit_source(source, self.config.sandbox)
        self.bus.publish("firewall.result", rep.to_dict())
        return rep

    def extract(self, source: str, targets: Optional[list[str]] = None) -> ModuleSpec:
        return self.spec_agent.extract(source, targets)

    def verify(self, source: str, targets: Optional[list[str]] = None) -> dict[str, Any]:
        return self._run(source, "verify", targets)

    def heal(self, source: str, targets: Optional[list[str]] = None) -> dict[str, Any]:
        return self._run(source, "heal", targets)

    def validate(self, source: str, spec: ModuleSpec, trials: Optional[int] = None,
                 seed: int = 424242) -> dict[str, Any]:
        """Independent post-repair validation: fresh seed, large budget, every target."""
        trials = trials or self.config.cegis.final_validation_trials
        per: dict[str, Any] = {}
        ok = True
        for t in spec.targets:
            res = self.breaker.attack(source, t, seed=seed, trials=trials, shrink=False)
            passed = bool(res.get("ok")) and not res.get("counterexamples")
            ok = ok and passed
            per[target_key(t)] = {"passed": passed, "trials": res.get("trials", 0),
                                  "error": res.get("error") or res.get("load_error"),
                                  "violations": [c["property"] for c in res.get("counterexamples", [])]}
        return {"passed": ok, "trials_per_target": trials, "seed": seed, "targets": per}

    # ------------------------------------------------------------------ graph
    def _graph(self) -> StateGraph[CegisState]:
        g: StateGraph[CegisState] = StateGraph("cegis", self.bus)
        g.add_node("firewall", self._n_firewall)
        g.add_node("spec", self._n_spec)
        g.add_node("select", self._n_select)
        g.add_node("break", self._n_break)
        g.add_node("build", self._n_build)
        g.set_entry("firewall")
        g.add_conditional_edges("firewall", lambda s: "spec" if s.status == "running" else END, {"spec", END})
        g.add_edge("spec", "select")
        g.add_conditional_edges("select", lambda s: "break" if s.current is not None else END, {"break", END})
        g.add_conditional_edges("break", self._route_break, {"build", "select"})
        g.add_conditional_edges("build", lambda s: "break" if s.pending_candidate else "select", {"break", "select"})
        return g

    def _run(self, source: str, mode: str, targets: Optional[list[str]]) -> dict[str, Any]:
        t0 = time.perf_counter()
        session = self.ledger.new_session(mode)
        state = CegisState(source_original=source, source_current=source, mode=mode, session=session,
                           requested_targets=targets)
        max_steps = 4 + 3 * 50 * (self.config.cegis.max_rounds + 2)
        state = self._graph().run(state, max_steps=max_steps)
        return self._report(state, (time.perf_counter() - t0) * 1000)

    # ------------------------------------------------------------------ nodes
    def _n_firewall(self, s: CegisState) -> CegisState:
        s.audit = self.audit(s.source_original)
        if not s.audit.allowed:
            s.status = "rejected"
        return s

    def _n_spec(self, s: CegisState) -> CegisState:
        s.spec = self.extract(s.source_original, s.requested_targets)
        s.queue = list(s.spec.targets)
        self.bus.publish("spec.ready", {"targets": [
            {"target": target_key(t), "properties": [{"name": p.name, "kind": p.kind, "origin": p.origin,
                                                      "description": p.description} for p in t.properties],
             "notes": t.notes} for t in s.spec.targets]})
        return s

    def _n_select(self, s: CegisState) -> CegisState:
        s.current = s.queue.pop(0) if s.queue else None
        s.round = 0
        s.pending_candidate = None
        if s.current is not None:
            s.outcomes.append(TargetOutcome(
                target=target_key(s.current), notes=list(s.current.notes),
                properties=[{"name": p.name, "kind": p.kind, "origin": p.origin, "description": p.description,
                             "confidence": p.confidence} for p in s.current.properties]))
            self.bus.publish("target.start", {"target": target_key(s.current),
                                              "properties": len(s.current.properties)})
        return s

    def _n_break(self, s: CegisState) -> CegisState:
        t = s.current
        assert t is not None
        key = target_key(t)
        seed = self.config.fuzz.seed + 7919 * s.round
        res = self.breaker.attack(s.source_current, t, seed=seed)
        s.last_campaign = res
        out = s.outcome
        if not res.get("ok"):
            out.status, out.error = "error", res.get("error") or res.get("load_error")
            if s.pending_candidate:
                self.ledger.record_candidate(s.session, key, s.pending_candidate, "refuted", [])
                s.pending_candidate = None
            self.bus.publish("breaker.error", {"target": key, "error": out.error})
            return s
        cexs = res.get("counterexamples", [])
        summary = {k: res.get(k) for k in ("trials", "rejected", "elapsed_ms", "first_failure_trial",
                                           "first_failure_ms", "coverage_arcs")}
        if s.round == 0:
            out.first_campaign = summary
            out.counterexamples = cexs
        out.rounds.append({"round": s.round, "seed": seed, **summary,
                           "violations": [{"property": c["property"], "message": c["minimal_message"]}
                                          for c in cexs]})
        self.bus.publish("breaker.result", {"target": key, "round": s.round, **summary,
                                            "counterexamples": cexs})
        for c in cexs:
            self.ledger.add_counterexample(s.session, key, c["property"], c["minimal_message"], c["minimal_case"],
                                           s.round)
            if c["minimal_case"] != c["initial_case"]:
                self.ledger.add_counterexample(s.session, key, c["property"], c["message"], c["initial_case"],
                                               s.round)
        for fc in res.get("failing_cases", [])[:4]:
            self.ledger.add_counterexample(s.session, key, "?", "additional failing input", fc, s.round)
        if s.pending_candidate:
            status = "refuted" if cexs else "verified"
            self.ledger.record_candidate(s.session, key, s.pending_candidate, status,
                                         (s.pending_repair or {}).get("edits", []))
            if cexs:
                self.bus.publish("cegis.refuted", {"target": key, "round": s.round})
                s.source_current = s.pending_repair["base_source"] if s.pending_repair else s.source_current
            s.pending_candidate = None
        if not cexs:
            out.status = "verified" if s.round == 0 else "repaired"
        elif s.mode == "verify":
            out.status = "falsified"
        elif s.round >= self.config.cegis.max_rounds:
            out.status = "unrepaired"
        return s

    def _route_break(self, s: CegisState) -> str:
        if s.outcome.status in ("verified", "repaired", "falsified", "unrepaired", "error"):
            return "select"
        return "build"

    def _n_build(self, s: CegisState) -> CegisState:
        t = s.current
        assert t is not None
        key = target_key(t)
        cases = [c["case"] for c in self.ledger.counterexamples(s.session, key)]
        violated = [(c["property"], c["minimal_message"]) for c in s.last_campaign.get("counterexamples", [])]
        base = s.source_current
        self.bus.publish("builder.start", {"target": key, "round": s.round, "counterexamples": len(cases)})
        rr = self.builder.repair(base, t, cases, s.last_campaign.get("passing", []), violated,
                                 refuted=self.ledger.refuted(s.session, key))
        info = rr.to_dict()
        info["round"] = s.round
        s.outcome.repairs.append(info)
        self.bus.publish("builder.result", {"target": key, "success": rr.success, "edits": rr.edits,
                                            "diff": rr.diff, "evaluations": rr.evaluations,
                                            "suspicious_lines": rr.suspicious_lines})
        if rr.success and rr.patched_source:
            s.pending_candidate = src_hash(rr.patched_source)
            s.pending_repair = {"edits": rr.edits, "base_source": base}
            self.ledger.record_candidate(s.session, key, s.pending_candidate, "pending", rr.edits, rr.best_reward)
            s.source_current = rr.patched_source
            s.round += 1
        else:
            s.outcome.status = "unrepaired"
            s.pending_candidate = None
        return s

    # ------------------------------------------------------------------ reporting
    def _report(self, s: CegisState, elapsed_ms: float) -> dict[str, Any]:
        statuses = [o.status for o in s.outcomes]
        if s.status == "rejected":
            overall = "rejected"
        elif any(st == "error" for st in statuses):
            overall = "error"
        elif s.mode == "verify":
            overall = "falsified" if "falsified" in statuses else "verified"
        else:
            if "unrepaired" in statuses:
                overall = "partially_repaired" if "repaired" in statuses else "unrepaired"
            else:
                overall = "repaired" if "repaired" in statuses else "verified"
        patched = s.source_current if s.source_current != s.source_original else None
        return {
            "mode": s.mode,
            "status": overall,
            "session": s.session,
            "elapsed_ms": round(elapsed_ms, 2),
            "audit": s.audit.to_dict() if s.audit else None,
            "skipped": s.spec.skipped if s.spec else [],
            "targets": [o.__dict__ for o in s.outcomes],
            "patched_source": patched,
            "diff": unified_diff(s.source_original, s.source_current) if patched else "",
            "ledger": self.ledger.stats(s.session),
            "spec": s.spec.to_dict() if s.spec else None,
        }
