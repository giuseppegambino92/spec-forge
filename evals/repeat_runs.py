"""Repeatability study: run the benchmark N consecutive times on a quiet machine and compare runs.

    python -m evals.repeat_runs [--runs 3] [--max-load-per-core 0.35] [--update-latest]

Protocol
  1. Before every run, wait until the 1-minute load average per CPU core is at or below
     ``--max-load-per-core`` (polled every ``--poll`` seconds, for at most ``--max-wait`` seconds).
     If the machine never becomes quiet, the study aborts (exit 3) unless ``--force`` is given;
     load averages before/after each run are recorded either way.
  2. Each run is a separate ``python -m evals.benchmark_suite`` process writing ``evals/runs/run_<i>.json``.
  3. Every run is reduced to a *fingerprint* of discrete outcomes (bugs found, counterexamples,
     minimal inputs, patches, verifier calls, oracle agreement, shrink complexities, firewall
     verdicts...). The engine is seeded, so fingerprints must be identical across runs.
     Concurrency cases (oracle == "atomicity") depend on OS thread scheduling: for those only the
     outcome fields must match; detail fields (minimal thread counts, shrink sizes) are reported
     as observed variation, not failures.
  4. Wall-clock metrics are summarised as min / median / max with the coefficient of variation,
     and the timing targets (shrink < 50 ms, audit < 2 ms) are checked on every run.

Exit codes: 0 repeatable, 1 a deterministic field differed or a run failed, 3 machine not quiet.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent

OUTCOME_FIELDS = ("found", "repair_success", "validation_passed", "oracle_agreement_pct", "naive_caught",
                  "false_alarm_on_reference", "ablation_success")


def load_per_core() -> float:
    return os.getloadavg()[0] / (os.cpu_count() or 1)


def wait_until_quiet(threshold: float, poll: float, max_wait: float) -> tuple[bool, float, float]:
    t0 = time.monotonic()
    while True:
        lpc = load_per_core()
        if lpc <= threshold:
            return True, lpc, time.monotonic() - t0
        if time.monotonic() - t0 >= max_wait:
            return False, lpc, time.monotonic() - t0
        print(f"  waiting for a quiet machine: load/core {lpc:.2f} > {threshold:.2f}", flush=True)
        time.sleep(poll)


# --------------------------------------------------------------------------- fingerprints


def case_fingerprint(c: dict[str, Any]) -> dict[str, Any]:
    d, r, ab = c["discovery"], c["repair"], c["repair_ablation_uniform_priors"]
    return {
        "outcome": {
            "found": d["found"],
            "naive_caught": c["naive"]["caught"],
            "false_alarm_on_reference": c["false_alarm_on_reference"],
            "repair_success": r["success"],
            "validation_passed": r.get("validation", {}).get("passed"),
            "oracle_agreement_pct": r.get("oracle", {}).get("agreement_pct"),
            "ablation_success": ab["success"],
        },
        "detail": {
            "counterexamples": [(x["property"], x["minimal_case"], x["true_positive"]) for x in d["counterexamples"]],
            "trials_to_first_failure": d["trials_to_first_failure"],
            "edits": r["edits"],
            "verifier_calls": r["verifier_calls"],
            "cegis_rounds": r["cegis_rounds"],
            "diff": r["diff"],
            "ablation_verifier_calls": ab["verifier_calls"],
            "validation_trials": [t["trials"] for t in r.get("validation", {}).get("targets", {}).values()],
        },
    }


def run_fingerprint(report: dict[str, Any]) -> dict[str, Any]:
    stochastic = {c["id"] for c in report["cases"] if c.get("oracle_mode") == "atomicity"}
    shrink = {kind: [(s["id"], s["property"], s["initial"], s["final"]) for s in rows]
              for kind, rows in report["shrink_samples"].items()}
    return {
        "cases": {c["id"]: case_fingerprint(c) for c in report["cases"]},
        "stochastic_cases": sorted(stochastic),
        "shrink": shrink,
        "firewall": [(f["payload"], f["blocked"], f["rules"]) for f in report["firewall_payloads"]],
        "benign": [(b["module"], b["allowed"]) for b in report["firewall_benign"]],
    }


def compare(fps: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Returns (violations, observed_stochastic_variation)."""
    violations: list[str] = []
    variation: list[str] = []
    base = fps[0]
    stochastic = set(base["stochastic_cases"])
    for i, fp in enumerate(fps[1:], start=2):
        for cid, cfp in base["cases"].items():
            other = fp["cases"].get(cid)
            if other is None:
                violations.append(f"run {i}: case {cid} missing")
                continue
            for k in OUTCOME_FIELDS:
                if cfp["outcome"][k] != other["outcome"][k]:
                    violations.append(f"run {i}: {cid} outcome `{k}` {cfp['outcome'][k]!r} != {other['outcome'][k]!r}")
            for k, v in cfp["detail"].items():
                if v != other["detail"][k]:
                    msg = f"run {i}: {cid} detail `{k}` differs ({_short(v)} vs {_short(other['detail'][k])})"
                    (variation if cid in stochastic else violations).append(msg)
        for kind in base["shrink"]:
            a = [s for s in base["shrink"][kind] if s[0] not in stochastic]
            b = [s for s in fp["shrink"][kind] if s[0] not in stochastic]
            if a != b:
                violations.append(f"run {i}: deterministic {kind} shrink samples differ")
            sa = [s for s in base["shrink"][kind] if s[0] in stochastic]
            sb = [s for s in fp["shrink"][kind] if s[0] in stochastic]
            if sa != sb:
                variation.append(f"run {i}: {kind} shrink samples of {sorted(stochastic)} differ "
                                 f"({_short(sa)} vs {_short(sb)})")
        for key in ("firewall", "benign"):
            if base[key] != fp[key]:
                violations.append(f"run {i}: {key} verdicts differ")
    return violations, variation


def _display_path(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


def _short(v: Any, n: int = 90) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."


# --------------------------------------------------------------------------- timing statistics


def timing_metrics(report: dict[str, Any]) -> dict[str, float]:
    s = report["summary"]
    sh_st = s["shrinking"]["stress_protocol_large_inputs"]
    sh_ca = s["shrinking"]["campaign_counterexamples"]
    fw = s["firewall"]
    return {
        "wall_time_s": report["wall_time_s"],
        "median_ms_to_first_failure": s["silent_bug_discovery"]["median_ms_to_first_failure"],
        "stress_shrink_median_ms": sh_st["median_ms"],
        "stress_shrink_p95_ms": sh_st["p95_ms"],
        "stress_shrink_max_ms": sh_st["max_ms"],
        "stress_shrink_under_50ms_pct": sh_st["under_50ms_pct"],
        "campaign_shrink_median_ms": sh_ca["median_ms"],
        "campaign_shrink_max_ms": sh_ca["max_ms"],
        "median_heal_ms": s["cegis_repair"]["median_heal_ms"],
        "firewall_mean_audit_ms": fw["mean_audit_ms"],
        "firewall_max_single_audit_ms": fw["max_single_audit_ms"],
    }


def summarise(values: list[float]) -> dict[str, float]:
    mean = statistics.fmean(values)
    return {"min": round(min(values), 4), "median": round(statistics.median(values), 4),
            "max": round(max(values), 4),
            "cv_pct": round(100 * statistics.pstdev(values) / mean, 2) if mean else 0.0}


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-load-per-core", type=float, default=0.35)
    ap.add_argument("--poll", type=float, default=10.0)
    ap.add_argument("--max-wait", type=float, default=900.0)
    ap.add_argument("--cooldown", type=float, default=5.0, help="seconds to idle between runs")
    ap.add_argument("--force", action="store_true", help="run even if the machine never becomes quiet")
    ap.add_argument("--quick", action="store_true", help="pass --quick to the benchmark (smoke test)")
    ap.add_argument("--runs-dir", default=str(ROOT / "runs"))
    ap.add_argument("--output", default=str(ROOT / "repeatability_report.json"))
    ap.add_argument("--update-latest", action="store_true",
                    help="copy the final run to evals/latest_report.json")
    args = ap.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    run_meta: list[dict[str, Any]] = []
    print(f"repeatability study: {args.runs} runs, quiet threshold {args.max_load_per_core:.2f} load/core "
          f"({os.cpu_count()} cores)")
    for i in range(1, args.runs + 1):
        if i > 1 and args.cooldown > 0:
            time.sleep(args.cooldown)
        quiet, lpc, waited = wait_until_quiet(args.max_load_per_core, args.poll, args.max_wait)
        if not quiet and not args.force:
            print(f"machine not quiet after {waited:.0f}s (load/core {lpc:.2f}); aborting (use --force)")
            return 3
        out = runs_dir / f"run_{i}.json"
        cmd = [sys.executable, "-m", "evals.benchmark_suite", "--output", str(out)] + (["--quick"] if args.quick else [])
        print(f"run {i}/{args.runs}: load/core {lpc:.2f} (waited {waited:.0f}s) -> {out.name}", flush=True)
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
        elapsed = time.perf_counter() - t0
        if proc.returncode != 0:
            print(proc.stdout[-2000:], proc.stderr[-4000:], sep="\n")
            print(f"run {i} failed with exit code {proc.returncode}")
            return 1
        reports.append(json.loads(out.read_text()))
        run_meta.append({"run": i, "load_per_core_before": round(lpc, 3), "quiet": quiet,
                         "load_per_core_after": round(load_per_core(), 3), "waited_s": round(waited, 1),
                         "process_s": round(elapsed, 2), "report": _display_path(out)})

    fps = [run_fingerprint(r) for r in reports]
    violations, variation = compare(fps)
    timings = {k: summarise([timing_metrics(r)[k] for r in reports]) for k in timing_metrics(reports[0])}
    targets = {
        "stress_shrink_reduction_gt_80pct_every_run": all(
            r["summary"]["shrinking"]["stress_protocol_large_inputs"]["aggregate_reduction_pct"] > 80 for r in reports),
        "firewall_every_audit_lt_2ms_every_run": all(
            r["summary"]["firewall"]["max_single_audit_ms"] < 2.0 for r in reports),
        "discovery_ge_90pct_every_run": all(
            r["summary"]["silent_bug_discovery"]["specforge_rate_pct"] >= 90 for r in reports),
        "repair_ge_85pct_every_run": all(r["summary"]["cegis_repair"]["repair_rate_pct"] >= 85 for r in reports),
        "firewall_block_100pct_every_run": all(r["summary"]["firewall"]["block_rate_pct"] == 100 for r in reports),
    }
    first = reports[0]["summary"]
    study = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runs": args.runs,
        "quick": args.quick,
        "environment": reports[0]["environment"],
        "cpu_count": os.cpu_count(),
        "quiet_threshold_load_per_core": args.max_load_per_core,
        "run_meta": run_meta,
        "repeatable": not violations,
        "deterministic_violations": violations,
        "stochastic_cases": fps[0]["stochastic_cases"],
        "observed_stochastic_variation": variation,
        "outcome_summary_run_1": {
            "specforge_caught": first["silent_bug_discovery"]["specforge_caught"],
            "naive_caught": first["silent_bug_discovery"]["naive_unit_tests_caught"],
            "false_alarms": first["silent_bug_discovery"]["false_alarms_on_reference_fixes"],
            "repaired": first["cegis_repair"]["repaired"],
            "oracle_exact": first["cegis_repair"]["oracle_exact_agreement"],
            "firewall_blocked": first["firewall"]["blocked"],
            "benign_allowed": first["firewall"]["benign_allowed"],
            "stress_shrink_aggregate_reduction_pct": first["shrinking"]["stress_protocol_large_inputs"][
                "aggregate_reduction_pct"],
        },
        "deterministic_metric_values_per_run": [
            {"stress_aggregate_reduction_pct": r["summary"]["shrinking"]["stress_protocol_large_inputs"][
                "aggregate_reduction_pct"],
             "stress_mean_reduction_pct": r["summary"]["shrinking"]["stress_protocol_large_inputs"][
                 "mean_reduction_pct"],
             "campaign_aggregate_reduction_pct": r["summary"]["shrinking"]["campaign_counterexamples"][
                 "aggregate_reduction_pct"],
             "ablation_mean_verifier_calls": r["summary"]["cegis_repair"]["ablation_uniform_priors"][
                 "mean_verifier_calls"],
             "median_trials_to_first_failure": r["summary"]["silent_bug_discovery"]["median_trials_to_first_failure"]}
            for r in reports],
        "timing_across_runs": timings,
        "targets_met_every_run": targets,
    }
    Path(args.output).write_text(json.dumps(study, indent=2, default=str) + "\n", encoding="utf-8")
    if args.update_latest:
        shutil.copyfile(runs_dir / f"run_{args.runs}.json", ROOT / "latest_report.json")

    print("\n" + "=" * 78)
    print(f"repeatable: {'YES' if not violations else 'NO'}  ({len(violations)} deterministic differences, "
          f"{len(variation)} stochastic variations in {fps[0]['stochastic_cases']})")
    for v in violations[:20]:
        print("  ✗", v)
    for v in variation[:10]:
        print("  ~", v)
    for k, v in timings.items():
        print(f"  {k:<32} min {v['min']:<10} median {v['median']:<10} max {v['max']:<10} cv {v['cv_pct']}%")
    for k, v in targets.items():
        print(f"  {'✓' if v else '✗'} {k}")
    print(f"-> {args.output}")
    print("=" * 78)
    return 0 if not violations and all(targets.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
