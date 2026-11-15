"""Message catalog for the human-facing CLI / TUI output (English and Italian).

Only text meant for people goes through here. Machine-readable output (``--json`` reports,
MCP results, patched sources, diffs, ledger keys) and everything the engine itself produces
(property statements, counterexample messages, repair-edit descriptions, firewall rule
messages) stays untranslated, because other tools parse it and it is identical across
languages.

The language is chosen, in order of precedence, by :func:`set_language` (the CLI's global
``--lang`` option) or by the ``SPECFORGE_LANG`` environment variable; the default is English.
"""

from __future__ import annotations

import os
from string import Formatter
from typing import Any

ENV_VAR = "SPECFORGE_LANG"
DEFAULT = "en"

CATALOG: dict[str, dict[str, str]] = {
    "en": {
        # ---- CLI help (Typer)
        "help.app": "spec-forge: neuro-symbolic CEGIS verification & self-healing for Python.",
        "help.app.lang": "language of the human-readable output: en | it (default: $SPECFORGE_LANG, else en); "
        "JSON output is never translated",
        "help.verify": "Infer properties and adversarially falsify them. Exit code 1 if a counterexample is found.",
        "help.verify.target": "restrict to target(s)",
        "help.verify.trials": "adversarial inputs per target",
        "help.verify.inprocess": "skip the subprocess sandbox (trusted code only)",
        "help.verify.ledger": "ledger sqlite path",
        "help.heal": "Run the CEGIS loop and emit a property-verified patch.",
        "help.heal.write": "overwrite the file with the verified patch",
        "help.heal.out": "write the patched module here",
        "help.heal.llm": "optional neural policy: anthropic | openai | ollama",
        "help.heal.model": "model id for --llm",
        "help.heal.validate": "run the independent 1,000-trial validation",
        "help.audit": "Static sandbox-escape & prompt-injection audit (no code is executed).",
        "help.eval": "Run the 12-bug benchmark suite and write the JSON report.",
        "help.eval.output": "report path",
        "help.eval.quick": "smaller budgets (smoke run)",
        "help.dash": "Live terminal dashboard of the CEGIS loop.",
        "help.dash.mode": "heal | verify",
        "help.mcp": "Serve the MCP tools (specforge_verify / specforge_heal / specforge_audit) over stdio.",
        "help.version": "Print the version.",
        # ---- CLI messages
        "cli.no_such_file": "[red]no such file: {path}[/red]",
        "cli.not_repo_root": "[red]run from the repository root (evals/ package not importable)[/red]",
        "cli.wrote_patch": "wrote patched module to {dest}",
        "cli.final_status": "final status: [bold]{status}[/bold]",
        # ---- report rendering
        "report.header": "[bold {colour}]{status}[/]  mode={mode}  {elapsed_ms:.0f} ms  ledger={ledger}",
        "report.findings_title": "firewall findings",
        "col.rule": "rule",
        "col.severity": "severity",
        "col.line": "line",
        "col.message": "message",
        "col.property": "property",
        "col.origin": "origin",
        "col.statement": "statement",
        "report.minimal_input": "    minimal input: [bold]{case}[/bold]  (complexity {initial}→{final}",
        "report.shrink_tail": ", {pct:.0f}% smaller in {ms:.2f} ms)",
        "report.repair_round": "  {mark} repair round {round}: {edits} ({evaluations} verifier calls, {elapsed_ms:.0f} ms)",
        "report.no_candidate": "no candidate",
        "report.patch_title": "verified patch",
        "report.validation": "independent validation: {verdict} ({trials} fresh-seed trials per target)",
        "verdict.pass": "[green]PASS[/green]",
        "verdict.fail": "[red]FAIL[/red]",
        # ---- audit
        "audit.summary": "{verdict} ({elapsed_ms:.3f} ms, {count} finding(s))",
        "verdict.allow": "ALLOW",
        "verdict.block": "BLOCK",
        # ---- enumerations shown to people (JSON keeps the raw identifiers)
        "status.verified": "verified",
        "status.repaired": "repaired",
        "status.partially_repaired": "partially_repaired",
        "status.falsified": "falsified",
        "status.unrepaired": "unrepaired",
        "status.rejected": "rejected",
        "status.refuted": "refuted",
        "status.error": "error",
        "status.queued": "queued",
        "status.attacking": "attacking",
        "status.patched": "patched (re-attacking)",
        "origin.inferred": "inferred",
        "origin.docstring": "docstring",
        "severity.critical": "critical",
        "severity.high": "high",
        "severity.medium": "medium",
        # ---- TUI dashboard
        "tui.header_title": "spec-forge · CEGIS state graph",
        "tui.header_status": "   step {step}   target {target}   {elapsed:5.1f}s",
        "tui.props_title": "Spec Architect · inferred contracts",
        "tui.cex_title": "Breaker · delta-debugged counterexamples",
        "tui.mcts_title": "Builder · MCTS repair search",
        "tui.diff_title": "verified patch",
        "tui.log_title": "event log",
        "tui.col_target": "target",
        "tui.col_status": "status",
        "tui.col_minimal": "minimal counterexample",
        "tui.col_shrink": "shrink",
        "tui.col_edit_path": "edit path",
        "tui.col_reward": "reward",
        "tui.suspiciousness": "Ochiai suspiciousness: {lines}",
        "tui.no_patch": "no patch yet",
        "tui.log_firewall": "firewall: {verdict} in {elapsed_ms:.3f} ms",
        "tui.log_breaker": "breaker r{round} {target}: {trials} trials, {count} counterexample(s)",
        "tui.log_builder": "builder {target}: {outcome} after {evaluations} verifier calls",
        "tui.builder_success": "success",
        "tui.builder_failed": "failed",
        "tui.log_refuted": "CEGIS: candidate for {target} refuted, refining",
    },
    "it": {
        # ---- CLI help (Typer)
        "help.app": "spec-forge: verifica CEGIS neuro-simbolica e auto-riparazione per Python.",
        "help.app.lang": "lingua dell'output leggibile: en | it (predefinita: $SPECFORGE_LANG, altrimenti en); "
        "l'output JSON non viene mai tradotto",
        "help.verify": "Deduce le proprietà e cerca di falsificarle in modo avversariale. "
        "Exit code 1 se trova un controesempio.",
        "help.verify.target": "limita l'analisi ai target indicati",
        "help.verify.trials": "input avversariali per target",
        "help.verify.inprocess": "salta la sandbox a sottoprocesso (solo codice fidato)",
        "help.verify.ledger": "percorso del registro SQLite",
        "help.heal": "Esegue il ciclo CEGIS e produce una patch verificata sulle proprietà.",
        "help.heal.write": "sovrascrive il file con la patch verificata",
        "help.heal.out": "scrive qui il modulo corretto",
        "help.heal.llm": "politica neurale opzionale: anthropic | openai | ollama",
        "help.heal.model": "id del modello per --llm",
        "help.heal.validate": "esegue la validazione indipendente da 1.000 prove",
        "help.audit": "Audit statico contro evasioni dalla sandbox e prompt injection (nessun codice viene eseguito).",
        "help.eval": "Esegue il benchmark sui 12 bug e scrive il report JSON.",
        "help.eval.output": "percorso del report",
        "help.eval.quick": "budget ridotti (smoke run)",
        "help.dash": "Dashboard da terminale del ciclo CEGIS in tempo reale.",
        "help.dash.mode": "heal | verify",
        "help.mcp": "Espone gli strumenti MCP (specforge_verify / specforge_heal / specforge_audit) su stdio.",
        "help.version": "Mostra la versione.",
        # ---- CLI messages
        "cli.no_such_file": "[red]file non trovato: {path}[/red]",
        "cli.not_repo_root": "[red]eseguire dalla radice del repository (il package evals/ non è importabile)[/red]",
        "cli.wrote_patch": "modulo corretto scritto in {dest}",
        "cli.final_status": "stato finale: [bold]{status}[/bold]",
        # ---- report rendering
        "report.header": "[bold {colour}]{status}[/]  modalità={mode}  {elapsed_ms:.0f} ms  registro={ledger}",
        "report.findings_title": "segnalazioni del firewall",
        "col.rule": "regola",
        "col.severity": "gravità",
        "col.line": "riga",
        "col.message": "messaggio",
        "col.property": "proprietà",
        "col.origin": "origine",
        "col.statement": "enunciato",
        "report.minimal_input": "    input minimo: [bold]{case}[/bold]  (complessità {initial}→{final}",
        "report.shrink_tail": ", {pct:.0f}% più piccolo in {ms:.2f} ms)",
        "report.repair_round": "  {mark} round di riparazione {round}: {edits} "
        "(chiamate al verificatore: {evaluations}, {elapsed_ms:.0f} ms)",
        "report.no_candidate": "nessun candidato",
        "report.patch_title": "patch verificata",
        "report.validation": "validazione indipendente: {verdict} ({trials} prove con seed nuovi per target)",
        "verdict.pass": "[green]SUPERATA[/green]",
        "verdict.fail": "[red]NON SUPERATA[/red]",
        # ---- audit
        "audit.summary": "{verdict} ({elapsed_ms:.3f} ms, segnalazioni: {count})",
        "verdict.allow": "CONSENTITO",
        "verdict.block": "BLOCCATO",
        # ---- enumerations shown to people (JSON keeps the raw identifiers)
        "status.verified": "verificato",
        "status.repaired": "riparato",
        "status.partially_repaired": "riparato in parte",
        "status.falsified": "falsificato",
        "status.unrepaired": "non riparato",
        "status.rejected": "respinto",
        "status.refuted": "confutato",
        "status.error": "errore",
        "status.queued": "in coda",
        "status.attacking": "sotto attacco",
        "status.patched": "patch applicata (nuovo attacco)",
        "origin.inferred": "dedotta",
        "origin.docstring": "docstring",
        "severity.critical": "critica",
        "severity.high": "alta",
        "severity.medium": "media",
        # ---- TUI dashboard
        "tui.header_title": "spec-forge · grafo di stati CEGIS",
        "tui.header_status": "   passo {step}   target {target}   {elapsed:5.1f}s",
        "tui.props_title": "Spec Architect · contratti dedotti",
        "tui.cex_title": "Breaker · controesempi ridotti con delta debugging",
        "tui.mcts_title": "Builder · ricerca MCTS della riparazione",
        "tui.diff_title": "patch verificata",
        "tui.log_title": "log degli eventi",
        "tui.col_target": "target",
        "tui.col_status": "stato",
        "tui.col_minimal": "controesempio minimo",
        "tui.col_shrink": "riduzione",
        "tui.col_edit_path": "sequenza di modifiche",
        "tui.col_reward": "reward",
        "tui.suspiciousness": "sospettosità di Ochiai: {lines}",
        "tui.no_patch": "ancora nessuna patch",
        "tui.log_firewall": "firewall: {verdict} in {elapsed_ms:.3f} ms",
        "tui.log_breaker": "breaker r{round} {target}: {trials} prove, controesempi: {count}",
        "tui.log_builder": "builder {target}: {outcome} (chiamate al verificatore: {evaluations})",
        "tui.builder_success": "riuscito",
        "tui.builder_failed": "fallito",
        "tui.log_refuted": "CEGIS: candidato per {target} confutato, raffinamento in corso",
    },
}

LANGUAGES: tuple[str, ...] = tuple(CATALOG)

_current: str | None = None


def normalize(lang: str | None) -> str | None:
    """Map ``"it"``, ``"IT"``, ``"it_IT.UTF-8"``, ``"it-IT"`` to ``"it"``; unknown values to ``None``."""
    if not lang:
        return None
    code = lang.strip().lower().replace("-", "_").split(".")[0].split("_")[0]
    return code if code in CATALOG else None


def get_language() -> str:
    """The active language: the explicit choice, else ``SPECFORGE_LANG``, else English."""
    return _current or normalize(os.environ.get(ENV_VAR)) or DEFAULT


def set_language(lang: str | None) -> None:
    """Select the output language; ``None`` falls back to ``SPECFORGE_LANG`` / English."""
    global _current
    if lang is not None and normalize(lang) is None:
        raise ValueError(f"unsupported language {lang!r}; choose one of {', '.join(LANGUAGES)}")
    _current = normalize(lang)


def has(key: str) -> bool:
    return key in CATALOG[DEFAULT]


def t(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """Translate ``key`` and fill its ``{placeholders}`` with ``kwargs``."""
    template = CATALOG[normalize(lang) or get_language()].get(key, CATALOG[DEFAULT][key])
    return template.format(**kwargs) if kwargs else template


def label(kind: str, value: str, lang: str | None = None) -> str:
    """Human label for an enumerated value (status, origin, severity); unknown values pass through."""
    key = f"{kind}.{value}"
    return t(key, lang) if has(key) else value


def placeholders(template: str) -> set[tuple[str, str]]:
    """The ``(field, format_spec)`` pairs of a template (used by the catalog consistency test)."""
    return {(field, spec or "") for _, field, spec, _ in Formatter().parse(template) if field is not None}
