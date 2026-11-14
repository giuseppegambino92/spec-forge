"""Reproducible benchmark: naive unit tests vs. spec-forge CEGIS on 12 silent production bugs.

    python -m evals.benchmark_suite [--quick] [--output evals/latest_report.json]

Metrics
  1. Silent bug discovery    naive suites vs. Breaker (counterexamples confirmed as *true* bugs by
                             replaying them against the reference fix); false alarms on the 12 fixes
  2. Counterexample shrinking  complexity reduction + wall time, on campaign counterexamples and on a
                             large-input stress protocol (inputs forced to max size from trial 1)
  3. Autonomous CEGIS repair  patch must pass an independent 1,000-trial fresh-seed campaign, and is
                             additionally diffed against the reference fix on 1,000 inputs (oracle);
                             ablation with uniform MCTS priors and no fault localisation
  4. Firewall                20 adversarial payloads blocked + latency; benign modules allowed
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from spec_forge import __version__
from spec_forge.agents.breaker_agent import BreakerAgent, InputGenerator
from spec_forge.agents.spec_agent import ModuleSpec
from spec_forge.codec import sem_eq
from spec_forge.config import SpecForgeConfig
from spec_forge.orchestrator.cegis import SpecForge, target_key
from spec_forge.orchestrator.memory import CounterexampleLedger
from spec_forge.sandbox.firewall import audit_source
from spec_forge.sandbox.runner import SandboxRunner, load_module
from spec_forge.symbolic.constraints import ConstraintSet
from spec_forge.symbolic.invariants import CheckContext, build_properties, check_case

from .datasets import CASES, BugCase
from .datasets.adversarial_payloads import PAYLOADS
from .datasets.naive_suites import NAIVE_SUITES

ROOT = Path(__file__).resolve().parent


def pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def q(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return round(s[k], 3)


# --------------------------------------------------------------------------- 1. naive unit tests


def run_naive(case: BugCase, source: str) -> dict[str, Any]:
    ns: dict[str, Any] = {"__name__": f"naive_{case.id}"}
    exec(compile(source, case.module, "exec"), ns)  # noqa: S102 - trusted benchmark corpus
    failures = []
    for stmt in NAIVE_SUITES[case.id]:
        try:
            exec(stmt, dict(ns))  # noqa: S102
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{stmt} -> {type(exc).__name__}: {exc}")
    return {"tests": len(NAIVE_SUITES[case.id]), "failed": len(failures), "caught": bool(failures),
            "failures": failures}


# --------------------------------------------------------------------------- oracle


def oracle_check(case: BugCase, patched: str, reference: str, cfg: SpecForgeConfig, n: int,
                 seed: int, spec: ModuleSpec) -> dict[str, Any]:
    """Differential testing of the patched module against the reference fix (ground truth).

    Inputs are drawn from the contract of the code under repair (``spec``, extracted from the
    buggy module), so the patched code is only judged on the domain it promised to handle.
    """
    pns = load_module(patched, cfg.sandbox)
    rns = load_module(reference, cfg.sandbox)
    total = agree = 0
    first_mismatch = None
    for t in spec.targets:
        if t.kind == "class":
            ctx = CheckContext(pns, t, timeout_s=2.0)
            props = build_properties(ctx)
            rng = random.Random(seed)
            runs = max(50, n // 10)
            for _ in range(runs):
                c = {"threads": rng.randint(2, 8), "calls": rng.randint(1, 60), "args": {}, "init": {}}
                total += 1
                if not check_case(ctx, props, c):
                    agree += 1
                elif first_mismatch is None:
                    first_mismatch = {"target": t.name, "case": str(c)}
            continue
        ctx_r = CheckContext(rns, t, timeout_s=1.0)
        ctx_p = CheckContext(pns, t, timeout_s=1.0)
        rng = random.Random(seed)
        gen = InputGenerator(t.params, ConstraintSet.from_dict(t.constraints), rng, cfg.fuzz.max_collection_len)
        done = attempts = 0
        while done < n and attempts < n * 40:
            attempts += 1
            c = gen.gen_case(rng.randint(0, cfg.fuzz.max_collection_len))
            if not ctx_r.precondition_holds(c):
                continue
            done += 1
            total += 1
            o_r, o_p = ctx_r.call(c), ctx_p.call(c)
            if o_r.exc is not None or o_p.exc is not None:
                same = (o_r.exc is not None and o_p.exc is not None and type(o_r.exc) is type(o_p.exc))
            elif case.oracle == "multiset":
                same = sorted(o_r.result) == sorted(o_p.result)
            else:
                same = sem_eq(o_r.result, o_p.result)
            if same:
                agree += 1
            elif first_mismatch is None:
                first_mismatch = {"target": t.name, "case": repr(c)[:200],
                                  "reference": repr(o_r.result if o_r.exc is None else o_r.exc)[:120],
                                  "patched": repr(o_p.result if o_p.exc is None else o_p.exc)[:120]}
    return {"inputs": total, "agree": agree, "agreement_pct": pct(agree, total), "first_mismatch": first_mismatch}


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", default=str(ROOT / "latest_report.json"))
    ap.add_argument("--quick", action="store_true", help="smaller budgets for a smoke run")
    ap.add_argument("--stress-seeds", type=int, default=3)
    args = ap.parse_args(argv)

    t_start = time.perf_counter()
    tmp = tempfile.mkdtemp(prefix="specforge-bench-")
    cfg = SpecForgeConfig(ledger_path=os.path.join(tmp, "ledger.db"))
    validation_trials = 200 if args.quick else cfg.cegis.final_validation_trials
    oracle_inputs = 200 if args.quick else 1000
    runner = SandboxRunner(cfg.sandbox)
    ledger = CounterexampleLedger(cfg.ledger_path)
    sf = SpecForge(cfg, runner=runner, ledger=ledger)
    ablation_cfg = SpecForgeConfig(ledger_path=cfg.ledger_path)
    ablation_cfg.mcts = replace(ablation_cfg.mcts, use_priors=False)
    sf_ablation = SpecForge(ablation_cfg, runner=runner, ledger=ledger)
    stress_breaker = BreakerAgent(runner, replace(cfg.fuzz, min_size=cfg.fuzz.max_collection_len), cfg.shrink)

    per_case: list[dict[str, Any]] = []
    native_shrinks: list[dict[str, Any]] = []
    stress_shrinks: list[dict[str, Any]] = []
    print(f"spec-forge {__version__} benchmark :: {len(CASES)} bug classes :: sandbox={cfg.sandbox.isolation}")

    for case in CASES:
        buggy, ref = case.buggy_source(), case.reference_source()
        row: dict[str, Any] = {"id": case.id, "module": case.module, "bug_class": case.bug_class,
                               "domain": case.domain, "oracle_mode": case.oracle}
        phase_ms: dict[str, float] = {}
        mark = [time.perf_counter()]

        def tick(name: str) -> None:
            now = time.perf_counter()
            phase_ms[name] = round((now - mark[0]) * 1000, 1)
            mark[0] = now

        # 1. naive unit tests
        row["naive"] = run_naive(case, buggy)
        row["naive_on_reference"] = run_naive(case, ref)
        tick("naive")

        # 2. discovery + true-positive confirmation + false alarms on the fix
        t0 = time.perf_counter()
        vr = sf.verify(buggy)
        row["verify_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        spec = ModuleSpec.from_dict(vr["spec"])
        confirmed: list[dict[str, Any]] = []
        for out in vr["targets"]:
            tspec = next(t for t in spec.targets if target_key(t) == out["target"])
            for c in out["counterexamples"]:
                rep = runner.submit({"op": "replay", "source": ref, "spec": tspec.to_dict(),
                                     "cases": [c["minimal_case"]]})
                ref_fails = rep.get("ok") and any(n == c["property"] for n, _ in rep["results"][0])
                confirmed.append({"target": out["target"], "property": c["property"], "message": c["minimal_message"],
                                  "minimal_case": repr(c["minimal_case"])[:160], "true_positive": not ref_fails})
                native_shrinks.append({"id": case.id, "property": c["property"],
                                       "initial": c["initial_complexity"], "final": c["final_complexity"],
                                       "ms": c.get("shrink_ms", 0.0), "reduction": c.get("shrink_reduction", 0.0)})
        first = [o["first_campaign"] for o in vr["targets"] if o["counterexamples"]]
        row["discovery"] = {
            "status": vr["status"], "found": any(x["true_positive"] for x in confirmed),
            "counterexamples": confirmed,
            "trials_to_first_failure": min((f["first_failure_trial"] for f in first), default=None),
            "ms_to_first_failure": min((f["first_failure_ms"] for f in first), default=None),
        }
        tick("discovery")
        rr = sf.verify(ref)
        row["false_alarm_on_reference"] = rr["status"] != "verified"
        row["reference_status"] = rr["status"]
        tick("false_alarm_check")

        # 3. stress shrinking protocol (large inputs)
        for s in range(args.stress_seeds):
            for t in spec.targets:
                res = stress_breaker.attack(buggy, t, seed=1000 + 17 * s)
                for c in res.get("counterexamples", [])[:1]:
                    stress_shrinks.append({"id": case.id, "seed": s, "property": c["property"],
                                           "initial": c["initial_complexity"], "final": c["final_complexity"],
                                           "ms": c.get("shrink_ms", 0.0), "reduction": c.get("shrink_reduction", 0.0),
                                           "initial_case": repr(c["initial_case"])[:120],
                                           "minimal_case": repr(c["minimal_case"])[:120]})

        tick("stress_shrink")
        # 4. CEGIS repair + independent validation + oracle
        t0 = time.perf_counter()
        hr = sf.heal(buggy)
        heal_ms = (time.perf_counter() - t0) * 1000
        patched = hr.get("patched_source")
        repairs = [r for o in hr["targets"] for r in o["repairs"]]
        rep_row: dict[str, Any] = {
            "status": hr["status"], "heal_ms": round(heal_ms, 2), "diff": hr.get("diff", ""),
            "edits": [r["edits"] for r in repairs if r["success"]],
            "verifier_calls": sum(r["evaluations"] for r in repairs), "cegis_rounds": len(repairs),
        }
        tick("heal")
        if patched:
            val = sf.validate(patched, spec, trials=validation_trials, seed=987654)
            rep_row["validation"] = val
            tick("validation")
            rep_row["oracle"] = oracle_check(case, patched, ref, cfg, oracle_inputs, 4242, spec)
            rep_row["firewall_allows_patch"] = audit_source(patched).allowed
        rep_row["success"] = bool(patched) and hr["status"] == "repaired" and rep_row["validation"]["passed"]
        row["repair"] = rep_row

        ar = sf_ablation.heal(buggy)
        ab_patched = ar.get("patched_source")
        ab_val = sf_ablation.validate(ab_patched, spec, trials=validation_trials, seed=987654) if ab_patched else None
        ab_repairs = [r for o in ar["targets"] for r in o["repairs"]]
        row["repair_ablation_uniform_priors"] = {
            "status": ar["status"], "success": bool(ab_patched) and ar["status"] == "repaired" and bool(
                ab_val and ab_val["passed"]),
            "verifier_calls": sum(r["evaluations"] for r in ab_repairs),
            "oracle_agreement_pct": oracle_check(case, ab_patched, ref, cfg, oracle_inputs, 4242, spec)["agreement_pct"]
            if ab_patched else None,
        }
        tick("oracle_and_ablation")
        row["phase_ms"] = phase_ms
        per_case.append(row)
        d, r = row["discovery"], row["repair"]
        print(f"  {case.id} {case.bug_class:<42} naive={'caught' if row['naive']['caught'] else 'missed':<6} "
              f"breaker={'FOUND' if d['found'] else 'missed':<6} repair={'OK' if r['success'] else 'FAIL':<4} "
              f"oracle={r.get('oracle', {}).get('agreement_pct', '-')}% calls={r['verifier_calls']} "
              f"ablation_calls={row['repair_ablation_uniform_priors']['verifier_calls']}")

    # 5. firewall
    fw_rows = []
    for name, category, src in PAYLOADS:
        audit_source(src)  # warm-up
        times = []
        rep = None
        for _ in range(50):
            rep = audit_source(src)
            times.append(rep.elapsed_ms)
        assert rep is not None
        fw_rows.append({"payload": name, "category": category, "blocked": not rep.allowed,
                        "rules": sorted({f.rule for f in rep.blocking}), "mean_ms": round(statistics.fmean(times), 4),
                        "p99_ms": q(times, 0.99), "max_ms": round(max(times), 4)})
    benign = []
    for case in CASES:
        for label, src in (("buggy", case.buggy_source()), ("reference", case.reference_source())):
            rep = audit_source(src)
            benign.append({"module": f"{label}/{case.module}", "allowed": rep.allowed,
                           "findings": [f.message for f in rep.findings]})
    sf.close()
    sf_ablation.close()
    runner.close()

    n = len(CASES)
    discovered = [r for r in per_case if r["discovery"]["found"]]
    repaired = [r for r in discovered if r["repair"]["success"]]
    oracle_exact = [r for r in repaired if r["repair"].get("oracle", {}).get("agreement_pct") == 100.0]
    ab_ok = [r for r in discovered if r["repair_ablation_uniform_priors"]["success"]]

    def shrink_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {}
        init = sum(r["initial"] for r in rows)
        fin = sum(r["final"] for r in rows)
        return {"samples": len(rows), "mean_reduction_pct": round(100 * statistics.fmean(r["reduction"] for r in rows), 2),
                "aggregate_reduction_pct": pct(init - fin, init),
                "median_ms": q([r["ms"] for r in rows], 0.5), "p95_ms": q([r["ms"] for r in rows], 0.95),
                "max_ms": round(max(r["ms"] for r in rows), 3),
                "under_50ms_pct": pct(sum(1 for r in rows if r["ms"] < 50), len(rows))}

    summary = {
        "silent_bug_discovery": {
            "naive_unit_tests_caught": sum(1 for r in per_case if r["naive"]["caught"]),
            "naive_unit_tests_rate_pct": pct(sum(1 for r in per_case if r["naive"]["caught"]), n),
            "naive_tests_pass_on_reference": all(not r["naive_on_reference"]["caught"] for r in per_case),
            "specforge_caught": len(discovered), "specforge_rate_pct": pct(len(discovered), n),
            "true_positive_counterexamples": sum(1 for r in per_case for c in r["discovery"]["counterexamples"]
                                                 if c["true_positive"]),
            "total_counterexamples": sum(len(r["discovery"]["counterexamples"]) for r in per_case),
            "false_alarms_on_reference_fixes": sum(1 for r in per_case if r["false_alarm_on_reference"]),
            "median_trials_to_first_failure": q([r["discovery"]["trials_to_first_failure"] for r in discovered], 0.5),
            "median_ms_to_first_failure": q([r["discovery"]["ms_to_first_failure"] for r in discovered], 0.5),
        },
        "shrinking": {"campaign_counterexamples": shrink_summary(native_shrinks),
                      "stress_protocol_large_inputs": shrink_summary(stress_shrinks)},
        "cegis_repair": {
            "repaired": len(repaired), "of_discovered": len(discovered),
            "repair_rate_pct": pct(len(repaired), len(discovered)),
            "validation_trials_per_target": validation_trials,
            "oracle_exact_agreement": len(oracle_exact), "oracle_inputs_per_target": oracle_inputs,
            "median_verifier_calls": q([r["repair"]["verifier_calls"] for r in repaired], 0.5),
            "median_heal_ms": q([r["repair"]["heal_ms"] for r in repaired], 0.5),
            "ablation_uniform_priors": {
                "repaired": len(ab_ok), "repair_rate_pct": pct(len(ab_ok), len(discovered)),
                "median_verifier_calls": q([r["repair_ablation_uniform_priors"]["verifier_calls"] for r in ab_ok], 0.5),
                "mean_verifier_calls": round(statistics.fmean(
                    [r["repair_ablation_uniform_priors"]["verifier_calls"] for r in ab_ok]), 2) if ab_ok else None,
                "mean_verifier_calls_with_priors": round(statistics.fmean(
                    [r["repair"]["verifier_calls"] for r in repaired]), 2) if repaired else None,
            },
        },
        "firewall": {
            "payloads": len(fw_rows), "blocked": sum(1 for r in fw_rows if r["blocked"]),
            "block_rate_pct": pct(sum(1 for r in fw_rows if r["blocked"]), len(fw_rows)),
            "mean_audit_ms": round(statistics.fmean(r["mean_ms"] for r in fw_rows), 4),
            "max_mean_audit_ms": max(r["mean_ms"] for r in fw_rows),
            "p99_audit_ms": q([t for r in fw_rows for t in [r["p99_ms"]]], 1.0),
            "audits_under_2ms_pct": pct(sum(1 for r in fw_rows if r["max_ms"] < 2.0), len(fw_rows)),
            "max_single_audit_ms": max(r["max_ms"] for r in fw_rows),
            "benign_modules": len(benign), "benign_allowed": sum(1 for b in benign if b["allowed"]),
        },
    }
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "spec_forge_version": __version__,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "machine": platform.machine(), "sandbox": cfg.sandbox.isolation, "quick": args.quick},
        "config": {"fuzz_trials": cfg.fuzz.trials, "fuzz_seed": cfg.fuzz.seed, "mcts_iterations": cfg.mcts.iterations,
                   "mcts_max_depth": cfg.mcts.max_depth, "max_cegis_rounds": cfg.cegis.max_rounds},
        "wall_time_s": round(time.perf_counter() - t_start, 2),
        "summary": summary,
        "cases": per_case,
        "shrink_samples": {"campaign": native_shrinks, "stress": stress_shrinks},
        "firewall_payloads": fw_rows,
        "firewall_benign": benign,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    _print_summary(summary, report["wall_time_s"], args.output)
    return report


def _print_summary(s: dict[str, Any], wall: float, path: str) -> None:
    d, sh, rp, fw = s["silent_bug_discovery"], s["shrinking"], s["cegis_repair"], s["firewall"]
    st = sh["stress_protocol_large_inputs"]
    nat = sh["campaign_counterexamples"]
    ab = rp["ablation_uniform_priors"]
    lines = [
        "",
        "=" * 78,
        f"Silent bug discovery : naive unit tests {d['naive_unit_tests_caught']}/12 ({d['naive_unit_tests_rate_pct']}%)"
        f"  |  spec-forge {d['specforge_caught']}/12 ({d['specforge_rate_pct']}%)",
        f"                       {d['true_positive_counterexamples']}/{d['total_counterexamples']} counterexamples "
        f"confirmed against reference; false alarms on fixes: {d['false_alarms_on_reference_fixes']}/12",
        f"Shrinking (stress)   : {st.get('aggregate_reduction_pct')}% aggregate / {st.get('mean_reduction_pct')}% mean "
        f"reduction, median {st.get('median_ms')} ms, p95 {st.get('p95_ms')} ms, "
        f"{st.get('under_50ms_pct')}% < 50 ms (n={st.get('samples')})",
        f"Shrinking (campaign) : {nat.get('aggregate_reduction_pct')}% aggregate, median {nat.get('median_ms')} ms "
        f"(n={nat.get('samples')})",
        f"CEGIS repair         : {rp['repaired']}/{rp['of_discovered']} ({rp['repair_rate_pct']}%) pass "
        f"{rp['validation_trials_per_target']}-trial validation; oracle-exact {rp['oracle_exact_agreement']}"
        f"/{rp['repaired']}; median {rp['median_verifier_calls']} verifier calls",
        f"  ablation (uniform) : {ab['repaired']}/{rp['of_discovered']} repaired, mean "
        f"{ab['mean_verifier_calls']} vs {ab['mean_verifier_calls_with_priors']} verifier calls with priors",
        f"Firewall             : {fw['blocked']}/{fw['payloads']} blocked ({fw['block_rate_pct']}%), mean "
        f"{fw['mean_audit_ms']} ms, worst single {fw['max_single_audit_ms']} ms; benign allowed "
        f"{fw['benign_allowed']}/{fw['benign_modules']}",
        f"wall time {wall}s  ->  {path}",
        "=" * 78,
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1:])
