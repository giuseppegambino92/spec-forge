"""Rich live dashboard for the CEGIS loop.

Subscribes to the orchestrator's event bus and renders: state-graph position, inferred
properties per target, counterexamples with shrink progress (complexity before -> after),
the MCTS candidate stream, and the final verified diff.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..i18n import label, t
from ..orchestrator.state_graph import Event

NODES = ["firewall", "spec", "select", "break", "build"]


class Dashboard:
    def __init__(self) -> None:
        self.node = "-"
        self.steps = 0
        self.audit: dict[str, Any] = {}
        self.targets: list[dict[str, Any]] = []
        self.current = "-"
        self.cex: list[dict[str, Any]] = []
        self.candidates: list[dict[str, Any]] = []
        self.suspicious: list[Any] = []
        self.status: dict[str, str] = {}
        self.diff = ""
        self.log: list[str] = []
        self.started = time.time()
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ event ingestion
    def on_event(self, ev: Event) -> None:
        with self.lock:
            p = ev.payload
            k = ev.kind
            if k == "node.enter":
                self.node, self.steps = p["node"], p["step"]
            elif k == "firewall.result":
                self.audit = p
                self.log.append(t("tui.log_firewall", verdict=t("verdict.allow" if p["allowed"] else "verdict.block"),
                                  elapsed_ms=p["elapsed_ms"]))
            elif k == "spec.ready":
                self.targets = p["targets"]
            elif k == "target.start":
                self.current = p["target"]
                self.status[p["target"]] = "attacking"
            elif k == "breaker.result":
                for c in p["counterexamples"]:
                    self.cex.append({"target": p["target"], **c})
                n = len(p["counterexamples"])
                self.status[p["target"]] = "falsified" if n else ("verified" if p["round"] == 0 else "repaired")
                self.log.append(t("tui.log_breaker", round=p["round"], target=p["target"], trials=p.get("trials"),
                                  count=n))
            elif k == "builder.localized":
                self.suspicious = p["suspicious"]
            elif k == "builder.candidate":
                self.candidates.append(p)
            elif k == "builder.result":
                self.status[p["target"]] = "patched" if p["success"] else "unrepaired"
                if p["success"]:
                    self.diff = p["diff"]
                self.log.append(t("tui.log_builder", target=p["target"], evaluations=p["evaluations"],
                                  outcome=t("tui.builder_success" if p["success"] else "tui.builder_failed")))
            elif k == "cegis.refuted":
                self.log.append(t("tui.log_refuted", target=p["target"]))
            self.log = self.log[-8:]

    # ------------------------------------------------------------------ rendering
    def render(self) -> Layout:
        with self.lock:
            layout = Layout()
            layout.split_column(Layout(self._header(), size=3), Layout(name="body"), Layout(self._footer(), size=10))
            layout["body"].split_row(Layout(name="left"), Layout(name="right"))
            layout["body"]["left"].split_column(Layout(self._props()), Layout(self._cex()))
            layout["body"]["right"].split_column(Layout(self._mcts()), Layout(self._diff()))
            return layout

    def _header(self) -> Panel:
        line = Text()
        for n in NODES:
            style = "bold black on cyan" if n == self.node else "dim"
            line.append(f" {n} ", style=style)
            line.append(" → " if n != NODES[-1] else "", style="dim")
        line.append(t("tui.header_status", step=self.steps, target=self.current,
                      elapsed=time.time() - self.started), style="bold")
        return Panel(line, title=t("tui.header_title"), border_style="cyan")

    def _props(self) -> Panel:
        tab = Table(expand=True, show_lines=False)
        tab.add_column(t("tui.col_target"))
        tab.add_column(t("col.property"))
        tab.add_column(t("col.origin"), style="dim")
        tab.add_column(t("tui.col_status"))
        for tg in self.targets:
            st = self.status.get(tg["target"], "queued")
            colour = {"verified": "green", "repaired": "green", "falsified": "red", "unrepaired": "red"}.get(st, "yellow")
            for i, p in enumerate(tg["properties"]):
                tab.add_row(Text(tg["target"]) if i == 0 else "", Text(p["name"]), Text(label("origin", p["origin"])),
                            Text(label("status", st), style=colour) if i == 0 else "")
        return Panel(tab, title=t("tui.props_title"), border_style="blue")

    def _cex(self) -> Panel:
        tab = Table(expand=True)
        tab.add_column(t("col.property"))
        tab.add_column(t("tui.col_minimal"))
        tab.add_column(t("tui.col_shrink"), justify="right")
        for c in self.cex[-6:]:
            red = c.get("shrink_reduction")
            tab.add_row(Text(c["property"]), Text(str(c["minimal_case"])[:70]),
                        f"{c['initial_complexity']}→{c['final_complexity']} "
                        f"({(red or 0) * 100:.0f}%, {c.get('shrink_ms', 0):.1f}ms)")
        return Panel(tab, title=t("tui.cex_title"), border_style="red")

    def _mcts(self) -> Panel:
        tab = Table(expand=True)
        tab.add_column("#", justify="right")
        tab.add_column(t("tui.col_edit_path"))
        tab.add_column(t("tui.col_reward"), justify="right")
        for i, c in enumerate(self.candidates[-8:], start=max(1, len(self.candidates) - 7)):
            tab.add_row(str(i), Text(" ∘ ".join(c["edits"])[:80]), Text(f"{c['reward']:.3f}",
                                                                  style="green" if c["success"] else ""))
        sus = ", ".join(f"L{ln}:{s:.2f}" for ln, s in self.suspicious[:6]) or "-"
        return Panel(Group(Text(t("tui.suspiciousness", lines=sus), style="dim"), tab),
                     title=t("tui.mcts_title"), border_style="magenta")

    def _diff(self) -> Panel:
        body: Any = Syntax(self.diff, "diff", word_wrap=True) if self.diff else Text(t("tui.no_patch"), style="dim")
        return Panel(body, title=t("tui.diff_title"), border_style="green")

    def _footer(self) -> Panel:
        return Panel(Text("\n".join(self.log)), title=t("tui.log_title"), border_style="dim")


def run_dashboard(source: str, mode: str = "heal", forge: Any = None, console: Optional[Console] = None) -> dict:
    from ..orchestrator.cegis import SpecForge

    own = forge is None
    forge = forge or SpecForge()
    dash = Dashboard()
    unsubscribe = forge.bus.subscribe(dash.on_event)
    result: dict[str, Any] = {}

    def work() -> None:
        try:
            result["report"] = forge.heal(source) if mode == "heal" else forge.verify(source)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    th = threading.Thread(target=work, daemon=True)
    th.start()
    with Live(dash.render(), console=console, refresh_per_second=12, screen=False) as live:
        while th.is_alive():
            live.update(dash.render())
            time.sleep(0.08)
        live.update(dash.render())
    unsubscribe()
    if own:
        forge.close()
    if "error" in result:
        raise result["error"]
    return result["report"]
