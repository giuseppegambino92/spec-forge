"""``specforge`` command line interface."""

from __future__ import annotations

import json
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.markup import escape as esc
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from typer.core import TyperGroup

from .. import __version__, i18n
from ..config import SpecForgeConfig
from ..i18n import label, t

# English help texts; LocalizedGroup swaps in the selected language when the arguments are parsed.
_HELP = i18n.CATALOG["en"]


def _sniff_lang(args: list[str]) -> str | None:
    """Find ``--lang`` among the group options (before the subcommand), else ``SPECFORGE_LANG``."""
    it = iter(args)
    for arg in it:
        if arg == "--lang":
            return i18n.normalize(next(it, None))
        if arg.startswith("--lang="):
            return i18n.normalize(arg.partition("=")[2])
        if not arg.startswith("-"):
            break
    return i18n.normalize(os.environ.get(i18n.ENV_VAR))


class LocalizedGroup(TyperGroup):
    """Typer group whose help texts follow ``--lang`` / ``SPECFORGE_LANG``.

    The language must be known before Click renders ``--help``, which happens while the
    arguments are parsed, so it is sniffed from the raw arguments here. The ``--lang``
    option itself (validated by Click) is declared on the app callback.
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        i18n.set_language(_sniff_lang(args))
        self._localize_help()
        return super().parse_args(ctx, args)

    def main(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().main(*args, **kwargs)
        finally:
            i18n.set_language(None)  # do not leak the choice into later in-process calls

    def _localize_help(self) -> None:
        self.help = t("help.app")
        for param in self.params:
            if i18n.has(f"help.app.{param.name}"):
                param.help = t(f"help.app.{param.name}")  # type: ignore[attr-defined]
        for name, cmd in self.commands.items():
            if i18n.has(f"help.{name}"):
                cmd.help = t(f"help.{name}")
            for param in cmd.params:
                if i18n.has(f"help.{name}.{param.name}"):
                    param.help = t(f"help.{name}.{param.name}")  # type: ignore[attr-defined]


class Lang(str, Enum):
    en = "en"
    it = "it"


app = typer.Typer(cls=LocalizedGroup, add_completion=False, no_args_is_help=True, help=_HELP["help.app"])
console = Console()


@app.callback()
def _root(lang: Annotated[Lang | None, typer.Option("--lang", case_sensitive=False,
                                                    help=_HELP["help.app.lang"])] = None) -> None:
    # Without --lang, i18n falls back to $SPECFORGE_LANG (unknown values mean English).
    i18n.set_language(lang.value if lang else None)


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
        console.print(t("cli.no_such_file", path=esc(str(path))))
        raise typer.Exit(2)
    return path.read_text(encoding="utf-8")


def render_report(report: dict[str, Any]) -> None:
    status = report["status"]
    colour = {"verified": "green", "repaired": "green", "falsified": "red", "rejected": "red",
              "unrepaired": "red", "error": "red"}.get(status, "yellow")
    console.print(Panel(t("report.header", colour=colour, status=esc(label("status", status).upper()),
                          mode=report["mode"], elapsed_ms=report["elapsed_ms"], ledger=esc(str(report["ledger"]))),
                        title="spec-forge"))
    audit = report.get("audit") or {}
    if audit.get("findings"):
        tab = Table(title=t("report.findings_title"))
        for col in ("rule", "severity", "line", "message"):
            tab.add_column(t(f"col.{col}"))
        for f in audit["findings"]:
            tab.add_row(esc(f["rule"]), label("severity", f["severity"]), str(f["line"]), esc(f["message"]))
        console.print(tab)
    for tg in report["targets"]:
        tab = Table(title=esc(f"{tg['target']} — {label('status', tg['status'])}"), show_lines=False, expand=True)
        for col in ("property", "origin", "statement"):
            tab.add_column(t(f"col.{col}"))
        for p in tg["properties"]:
            tab.add_row(esc(p["name"]), label("origin", p["origin"]), esc(p["description"]))
        console.print(tab)
        for c in tg["counterexamples"]:
            red = c.get("shrink_reduction")
            console.print(f"  [red]✗ {esc(c['property'])}[/red]: {esc(c['minimal_message'])}")
            console.print(t("report.minimal_input", case=esc(repr(c["minimal_case"])),
                            initial=c["initial_complexity"], final=c["final_complexity"])
                          + (t("report.shrink_tail", pct=red * 100, ms=c.get("shrink_ms", 0)) if red is not None else ")"))
        for r in tg["repairs"]:
            mark = "[green]✓[/green]" if r["success"] else "[red]✗[/red]"
            console.print(t("report.repair_round", mark=mark, round=r["round"],
                            edits=esc(" ∘ ".join(r["edits"])) or t("report.no_candidate"),
                            evaluations=r["evaluations"], elapsed_ms=r["elapsed_ms"]))
    if report.get("diff"):
        console.print(Panel(Syntax(report["diff"], "diff"), title=t("report.patch_title")))


@app.command(help=_HELP["help.verify"])
def verify(path: Path, target: list[str] = typer.Option(None, "--target", "-t", help=_HELP["help.verify.target"]),
           trials: Optional[int] = typer.Option(None, help=_HELP["help.verify.trials"]),
           seed: Optional[int] = typer.Option(None), as_json: bool = typer.Option(False, "--json"),
           inprocess: bool = typer.Option(False, help=_HELP["help.verify.inprocess"]),
           ledger: Optional[str] = typer.Option(None, help=_HELP["help.verify.ledger"])) -> None:
    from ..orchestrator.cegis import SpecForge

    with SpecForge(_config(trials, seed, inprocess, ledger)) as sf:
        report = sf.verify(_read(path), target or None)
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    else:
        render_report(report)
    raise typer.Exit(0 if report["status"] == "verified" else 1)


@app.command(help=_HELP["help.heal"])
def heal(path: Path, target: list[str] = typer.Option(None, "--target", "-t"),
         write: bool = typer.Option(False, "--write", help=_HELP["help.heal.write"]),
         out: Optional[Path] = typer.Option(None, help=_HELP["help.heal.out"]),
         trials: Optional[int] = typer.Option(None), seed: Optional[int] = typer.Option(None),
         llm: Optional[str] = typer.Option(None, help=_HELP["help.heal.llm"]),
         model: Optional[str] = typer.Option(None, help=_HELP["help.heal.model"]),
         validate: bool = typer.Option(True, help=_HELP["help.heal.validate"]),
         as_json: bool = typer.Option(False, "--json"), inprocess: bool = typer.Option(False),
         ledger: Optional[str] = typer.Option(None)) -> None:
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
            console.print(t("report.validation", verdict=t("verdict.pass" if v["passed"] else "verdict.fail"),
                            trials=v["trials_per_target"]))
    patched = report.get("patched_source")
    if patched and (write or out):
        dest = out or path
        dest.write_text(patched, encoding="utf-8")
        console.print(t("cli.wrote_patch", dest=esc(str(dest))))
    raise typer.Exit(0 if report["status"] in ("verified", "repaired") else 1)


@app.command(help=_HELP["help.audit"])
def audit(path: Path, as_json: bool = typer.Option(False, "--json")) -> None:
    from ..sandbox.firewall import audit_source

    rep = audit_source(_read(path))
    if as_json:
        sys.stdout.write(json.dumps(rep.to_dict(), indent=2) + "\n")
    else:
        verdict = f"[green]{t('verdict.allow')}[/green]" if rep.allowed else f"[red]{t('verdict.block')}[/red]"
        console.print(t("audit.summary", verdict=verdict, elapsed_ms=rep.elapsed_ms, count=len(rep.findings)))
        for f in rep.findings:
            console.print(f"  L{f.line} \\[{label('severity', f.severity)}] {esc(f.rule)}: {esc(f.message)}")
    raise typer.Exit(0 if rep.allowed else 1)


@app.command("eval", help=_HELP["help.eval"])
def eval_cmd(output: Path = typer.Option(Path("evals/latest_report.json"), help=_HELP["help.eval.output"]),
             quick: bool = typer.Option(False, help=_HELP["help.eval.quick"])) -> None:
    try:
        from evals.benchmark_suite import main as bench_main
    except ImportError:
        console.print(t("cli.not_repo_root"))
        raise typer.Exit(2)
    bench_main(["--output", str(output)] + (["--quick"] if quick else []))


@app.command(help=_HELP["help.dash"])
def dash(path: Path, mode: str = typer.Option("heal", help=_HELP["help.dash.mode"])) -> None:
    from .tui import run_dashboard

    report = run_dashboard(_read(path), mode=mode, console=console)
    if not console.is_terminal:
        console.line()  # Rich's Live ends its last frame with a newline only on a real terminal
    console.print(t("cli.final_status", status=esc(label("status", report["status"]))))


@app.command(help=_HELP["help.mcp"])
def mcp() -> None:
    from .mcp_server import serve

    serve()


@app.command(help=_HELP["help.version"])
def version() -> None:
    console.print(f"spec-forge {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
