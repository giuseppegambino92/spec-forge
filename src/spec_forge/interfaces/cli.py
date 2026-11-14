"""``specforge`` command line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.markup import escape as esc
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .. import __version__
from ..config import SpecForgeConfig

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="spec-forge: neuro-symbolic CEGIS verification & self-healing for Python.")
console = Console()


def _config(trials: Optional[int], seed: Optional[int], inprocess: bool, ledger: Optional[str]) -> SpecForgeConfig:
    cfg = SpecForgeConfig()
    if trials:
        cfg.fuzz.trials = trials
    if seed is not None:
        cfg.fuzz.seed = seed
    if inprocess:
        cfg.sandbox.isolation = "inprocess"
    if ledger:
        cfg.ledger_path = ledger
    return cfg


def _policy(llm: Optional[str], model: Optional[str]) -> Any:
    if not llm:
        return None
    from ..llm.adapters import make_adapter
    from ..llm.policy import LLMRepairPolicy

    return LLMRepairPolicy(make_adapter(llm, model))


def _read(path: Path) -> str:
    if not path.exists():
        console.print(f"[red]no such file: {path}[/red]")
        raise typer.Exit(2)
    return path.read_text(encoding="utf-8")


def render_report(report: dict[str, Any]) -> None:
    status = report["status"]
    colour = {"verified": "green", "repaired": "green", "falsified": "red", "rejected": "red",
              "unrepaired": "red", "error": "red"}.get(status, "yellow")
    console.print(Panel(f"[bold {colour}]{status.upper()}[/]  mode={report['mode']}  "
                        f"{report['elapsed_ms']:.0f} ms  ledger={report['ledger']}", title="spec-forge"))
    audit = report.get("audit") or {}
    if audit.get("findings"):
        tab = Table(title="firewall findings")
        for col in ("rule", "severity", "line", "message"):
            tab.add_column(col)
        for f in audit["findings"]:
            tab.add_row(esc(f["rule"]), f["severity"], str(f["line"]), esc(f["message"]))
        console.print(tab)
    for t in report["targets"]:
        tab = Table(title=esc(f"{t['target']} — {t['status']}"), show_lines=False, expand=True)
        tab.add_column("property")
        tab.add_column("origin")
        tab.add_column("statement")
        for p in t["properties"]:
            tab.add_row(esc(p["name"]), p["origin"], esc(p["description"]))
        console.print(tab)
        for c in t["counterexamples"]:
            red = c.get("shrink_reduction")
            console.print(f"  [red]✗ {esc(c['property'])}[/red]: {esc(c['minimal_message'])}")
            console.print(f"    minimal input: [bold]{esc(repr(c['minimal_case']))}[/bold]  "
                          f"(complexity {c['initial_complexity']}→{c['final_complexity']}"
                          + (f", {red * 100:.0f}% smaller in {c.get('shrink_ms', 0):.2f} ms)" if red is not None else ")"))
        for r in t["repairs"]:
            mark = "[green]✓[/green]" if r["success"] else "[red]✗[/red]"
            console.print(f"  {mark} repair round {r['round']}: {esc(' ∘ '.join(r['edits'])) or 'no candidate'} "
                          f"({r['evaluations']} verifier calls, {r['elapsed_ms']:.0f} ms)")
    if report.get("diff"):
        console.print(Panel(Syntax(report["diff"], "diff"), title="verified patch"))


@app.command()
def verify(path: Path, target: list[str] = typer.Option(None, "--target", "-t", help="restrict to target(s)"),
           trials: Optional[int] = typer.Option(None, help="adversarial inputs per target"),
           seed: Optional[int] = typer.Option(None), as_json: bool = typer.Option(False, "--json"),
           inprocess: bool = typer.Option(False, help="skip the subprocess sandbox (trusted code only)"),
           ledger: Optional[str] = typer.Option(None, help="ledger sqlite path")) -> None:
    """Infer properties and adversarially falsify them. Exit code 1 if a counterexample is found."""
    from ..orchestrator.cegis import SpecForge

    with SpecForge(_config(trials, seed, inprocess, ledger)) as sf:
        report = sf.verify(_read(path), target or None)
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    else:
        render_report(report)
    raise typer.Exit(0 if report["status"] == "verified" else 1)


@app.command()
def heal(path: Path, target: list[str] = typer.Option(None, "--target", "-t"),
         write: bool = typer.Option(False, "--write", help="overwrite the file with the verified patch"),
         out: Optional[Path] = typer.Option(None, help="write the patched module here"),
         trials: Optional[int] = typer.Option(None), seed: Optional[int] = typer.Option(None),
         llm: Optional[str] = typer.Option(None, help="optional neural policy: anthropic | openai | ollama"),
         model: Optional[str] = typer.Option(None, help="model id for --llm"),
         validate: bool = typer.Option(True, help="run the independent 1,000-trial validation"),
         as_json: bool = typer.Option(False, "--json"), inprocess: bool = typer.Option(False),
         ledger: Optional[str] = typer.Option(None)) -> None:
    """Run the CEGIS loop and emit a property-verified patch."""
    from ..orchestrator.cegis import SpecForge

    source = _read(path)
    with SpecForge(_config(trials, seed, inprocess, ledger), policy=_policy(llm, model)) as sf:
        report = sf.heal(source, target or None)
        if validate and report.get("patched_source") and report["spec"]:
            from ..agents.spec_agent import ModuleSpec

            report["validation"] = sf.validate(report["patched_source"], ModuleSpec.from_dict(report["spec"]))
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    else:
        render_report(report)
        if "validation" in report:
            v = report["validation"]
            console.print(f"independent validation: {'[green]PASS[/green]' if v['passed'] else '[red]FAIL[/red]'} "
                          f"({v['trials_per_target']} fresh-seed trials per target)")
    patched = report.get("patched_source")
    if patched and (write or out):
        dest = out or path
        dest.write_text(patched, encoding="utf-8")
        console.print(f"wrote patched module to {dest}")
    raise typer.Exit(0 if report["status"] in ("verified", "repaired") else 1)


@app.command()
def audit(path: Path, as_json: bool = typer.Option(False, "--json")) -> None:
    """Static sandbox-escape & prompt-injection audit (no code is executed)."""
    from ..sandbox.firewall import audit_source

    rep = audit_source(_read(path))
    if as_json:
        sys.stdout.write(json.dumps(rep.to_dict(), indent=2) + "\n")
    else:
        verdict = "[green]ALLOW[/green]" if rep.allowed else "[red]BLOCK[/red]"
        console.print(f"{verdict} ({rep.elapsed_ms:.3f} ms, {len(rep.findings)} finding(s))")
        for f in rep.findings:
            console.print(f"  L{f.line} \\[{f.severity}] {esc(f.rule)}: {esc(f.message)}")
    raise typer.Exit(0 if rep.allowed else 1)


@app.command("eval")
def eval_cmd(output: Path = typer.Option(Path("evals/latest_report.json"), help="report path"),
             quick: bool = typer.Option(False, help="smaller budgets (smoke run)")) -> None:
    """Run the 12-bug benchmark suite and write the JSON report."""
    try:
        from evals.benchmark_suite import main as bench_main
    except ImportError:
        console.print("[red]run from the repository root (evals/ package not importable)[/red]")
        raise typer.Exit(2)
    bench_main(["--output", str(output)] + (["--quick"] if quick else []))


@app.command()
def dash(path: Path, mode: str = typer.Option("heal", help="heal | verify")) -> None:
    """Live terminal dashboard of the CEGIS loop."""
    from .tui import run_dashboard

    report = run_dashboard(_read(path), mode=mode, console=console)
    console.print(f"final status: [bold]{report['status']}[/bold]")


@app.command()
def mcp() -> None:
    """Serve the MCP tools (specforge_verify / specforge_heal / specforge_audit) over stdio."""
    from .mcp_server import serve

    serve()


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"spec-forge {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
