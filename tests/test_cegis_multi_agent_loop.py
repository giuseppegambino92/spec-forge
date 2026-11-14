"""Orchestration: state graph, ledger, Breaker/Builder agents and the full CEGIS loop."""

from __future__ import annotations

import pytest

from spec_forge.agents.breaker_agent import BreakerAgent
from spec_forge.agents.builder_agent import BuilderAgent, ochiai, src_hash
from spec_forge.agents.spec_agent import ModuleSpec, SpecAgent
from spec_forge.orchestrator.cegis import SpecForge
from spec_forge.orchestrator.memory import CounterexampleLedger
from spec_forge.orchestrator.state_graph import END, EventBus, GraphError, StateGraph
from spec_forge.sandbox.runner import SandboxRunner

QUOTA = '''
def deduct_quota(remaining: int, cost: int) -> int:
    """
    :requires: 0 <= remaining <= 2**32 - 1
    :requires: 0 <= cost <= 2**32 - 1
    :ensures: 0 <= result <= remaining
    """
    return remaining - cost
'''


# ----------------------------------------------------------------------------- state graph

def test_state_graph_runs_cycles_with_conditional_routing_and_events():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda e: seen.append(e.kind))
    g: StateGraph[dict] = StateGraph("loop", bus)
    g.add_node("inc", lambda s: {**s, "n": s["n"] + 1})
    g.add_node("check", lambda s: s)
    g.set_entry("inc")
    g.add_edge("inc", "check")
    g.add_conditional_edges("check", lambda s: "inc" if s["n"] < 5 else END, {"inc", END})
    assert g.run({"n": 0})["n"] == 5
    assert seen.count("node.enter") == 10 and seen[-1] == "graph.end"


def test_state_graph_budget_and_validation():
    g: StateGraph[dict] = StateGraph("forever")
    g.add_node("a", lambda s: s)
    g.set_entry("a")
    g.add_edge("a", "a")
    g.run({}, max_steps=7)
    assert any(e.kind == "graph.budget_exhausted" for e in g.bus.history)
    bad: StateGraph[dict] = StateGraph("bad")
    bad.add_node("x", lambda s: s)
    bad.set_entry("x")
    with pytest.raises(GraphError):
        bad.run({})  # no outgoing edge
    with pytest.raises(GraphError):
        bad.add_edge("x", "missing")
    r: StateGraph[dict] = StateGraph("router")
    r.add_node("x", lambda s: s)
    r.set_entry("x")
    r.add_conditional_edges("x", lambda s: "nowhere", {END})
    with pytest.raises(GraphError):
        r.run({})


# ----------------------------------------------------------------------------- ledger

def test_ledger_wal_dedup_and_refutation(tmp_path):
    led = CounterexampleLedger(str(tmp_path / "l.db"))
    assert led.journal_mode.lower() == "wal"
    sid = led.new_session("t")
    assert led.add_counterexample(sid, "f", "p", "m", {"x": (1, float("nan"))})
    assert not led.add_counterexample(sid, "f", "p", "m", {"x": (1, float("nan"))})  # dedup by content
    assert led.counterexamples(sid, "f")[0]["case"]["x"][0] == 1
    led.record_candidate(sid, "f", "h1", "pending", ["e"])
    led.record_candidate(sid, "f", "h1", "refuted", ["e"])
    assert led.refuted(sid, "f") == {"h1"}
    assert led.stats(sid) == {"counterexamples": 1, "candidates": 1, "refuted": 1}


# ----------------------------------------------------------------------------- agents

def test_breaker_finds_and_shrinks_quota_underflow(inprocess_config):
    runner = SandboxRunner(inprocess_config.sandbox)
    spec = SpecAgent().extract(QUOTA).targets[0]
    res = BreakerAgent(runner, inprocess_config.fuzz, inprocess_config.shrink).attack(QUOTA, spec)
    assert res["ok"] and res["counterexamples"]
    cex = res["counterexamples"][0]
    assert cex["property"] == "ensures[0]"
    assert cex["minimal_case"] == {"remaining": 0, "cost": 1}


def test_ochiai_ranks_the_faulty_line_first():
    runs = [{"failed": True, "lines": [1, 2, 3]}, {"failed": False, "lines": [1, 2]},
            {"failed": False, "lines": [1, 4]}]
    scores = ochiai(runs, [])
    assert max(scores, key=scores.get) == 3


def test_builder_respects_refuted_blacklist(inprocess_config):
    """Anti-oscillation: a refuted candidate is never proposed again."""
    runner = SandboxRunner(inprocess_config.sandbox)
    spec = SpecAgent().extract(QUOTA).targets[0]
    builder = BuilderAgent(runner, inprocess_config.mcts, inprocess_config.fuzz)
    cases = [{"remaining": 0, "cost": 1}]
    first = builder.repair(QUOTA, spec, cases, [{"remaining": 5, "cost": 1}], [("ensures[0]", "violated")])
    assert first.success
    second = builder.repair(QUOTA, spec, cases, [{"remaining": 5, "cost": 1}], [("ensures[0]", "violated")],
                            refuted={src_hash(first.patched_source)})
    assert second.success and second.patched_source != first.patched_source


class _ProposalPolicy:
    """A pluggable policy (the protocol an LLM policy implements) that proposes fixed rewrites."""

    def __init__(self, proposals):
        self.proposals = proposals
        self.calls = 0

    def propose(self, **kwargs):
        self.calls += 1
        return self.proposals


def test_policy_proposals_are_firewalled_and_verified(inprocess_config):
    """A bug outside the symbolic grammar: only the (neural) policy can fix it, and its malicious
    proposal must be stopped by the firewall before execution."""
    src = 'def shout(s: str) -> str:\n    """:ensures: result == s.upper()\n    """\n    return s.lower()\n'
    malicious = "import os\n" + src.replace("return s.lower()", "return os.getcwd()")
    good = src.replace("return s.lower()", "return s.upper()")
    policy = _ProposalPolicy([malicious, good])
    spec = SpecAgent().extract(src).targets[0]
    builder = BuilderAgent(SandboxRunner(inprocess_config.sandbox), inprocess_config.mcts, inprocess_config.fuzz,
                           policy=policy)
    res = builder.repair(src, spec, [{"s": "Ab"}], [{"s": ""}], [("ensures[0]", "violated")])
    assert policy.calls == 1
    assert res.rejected_by_firewall >= 1
    assert res.success and res.patched_source == good and res.categories == ["llm_rewrite"]


# ----------------------------------------------------------------------------- full CEGIS loop

@pytest.mark.parametrize("idx", range(12))
def test_cegis_heals_every_benchmark_bug(idx, cases, inprocess_config):
    case = cases[idx]
    with SpecForge(inprocess_config) as sf:
        report = sf.heal(case.buggy_source())
        assert report["status"] == "repaired", report["targets"]
        validation = sf.validate(report["patched_source"], ModuleSpec.from_dict(report["spec"]), trials=300)
    assert validation["passed"], validation
    assert report["diff"].startswith("--- a/target.py")


@pytest.mark.parametrize("idx", range(12))
def test_no_false_alarms_on_reference_fixes(idx, cases, inprocess_config):
    with SpecForge(inprocess_config) as sf:
        assert sf.verify(cases[idx].reference_source())["status"] == "verified"


def test_verify_mode_never_patches_and_reports_counterexamples(inprocess_config):
    with SpecForge(inprocess_config) as sf:
        rep = sf.verify(QUOTA)
    assert rep["status"] == "falsified" and rep["patched_source"] is None
    assert rep["targets"][0]["counterexamples"][0]["minimal_case"] == {"remaining": 0, "cost": 1}


def test_cegis_over_real_subprocess_sandbox(subprocess_config, cases):
    with SpecForge(subprocess_config) as sf:
        rep = sf.heal(cases[9].buggy_source())  # B10 input aliasing
        assert sf.runner.policy.isolation == "subprocess"
    assert rep["status"] == "repaired"
    assert "list(scores)" in rep["patched_source"]


def test_ablation_uniform_priors_still_repairs(inprocess_config, cases):
    inprocess_config.mcts.use_priors = False
    with SpecForge(inprocess_config) as sf:
        rep = sf.heal(cases[4].buggy_source())  # B05 zero division
    assert rep["status"] == "repaired"


def test_event_stream_covers_the_whole_loop(inprocess_config):
    kinds = []
    with SpecForge(inprocess_config) as sf:
        sf.bus.subscribe(lambda e: kinds.append(e.kind))
        sf.heal(QUOTA)
    for k in ("firewall.result", "spec.ready", "breaker.result", "builder.localized", "builder.candidate",
              "builder.result", "graph.end"):
        assert k in kinds, k
