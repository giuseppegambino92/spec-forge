"""Localisation of the human-facing CLI output (English default, Italian via --lang / SPECFORGE_LANG)."""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from spec_forge import i18n
from spec_forge.interfaces.cli import app

QUOTA = '''def deduct_quota(remaining: int, cost: int) -> int:
    """
    :requires: 0 <= remaining <= 2**32 - 1
    :requires: 0 <= cost <= 2**32 - 1
    :ensures: 0 <= result <= remaining
    """
    return remaining - cost
'''

MARKUP = re.compile(r"(?<!\\)\[(/?[a-z][a-z ]*|/)\]")


@pytest.fixture(autouse=True)
def _english_by_default(monkeypatch):
    monkeypatch.delenv(i18n.ENV_VAR, raising=False)
    i18n.set_language(None)
    yield
    i18n.set_language(None)


# ----------------------------------------------------------------------------- catalog


def test_both_languages_have_exactly_the_same_keys():
    assert set(i18n.LANGUAGES) == {"en", "it"}
    assert set(i18n.CATALOG["en"]) == set(i18n.CATALOG["it"])


@pytest.mark.parametrize("key", sorted(i18n.CATALOG["en"]))
def test_every_key_has_the_same_placeholders_and_markup(key):
    en, it = i18n.CATALOG["en"][key], i18n.CATALOG["it"][key]
    assert en.strip() and it.strip()
    assert i18n.placeholders(en) == i18n.placeholders(it)
    assert sorted(MARKUP.findall(en)) == sorted(MARKUP.findall(it))


def test_language_selection_and_fallbacks(monkeypatch):
    assert i18n.get_language() == "en"
    monkeypatch.setenv(i18n.ENV_VAR, "it_IT.UTF-8")
    assert i18n.get_language() == "it"
    i18n.set_language("en")  # explicit choice beats the environment
    assert i18n.get_language() == "en"
    i18n.set_language(None)
    monkeypatch.setenv(i18n.ENV_VAR, "fr")  # unknown values fall back to English
    assert i18n.get_language() == "en"
    with pytest.raises(ValueError):
        i18n.set_language("fr")
    assert i18n.t("cli.wrote_patch", lang="it", dest="x.py") == "modulo corretto scritto in x.py"
    assert i18n.label("status", "repaired", lang="it") == "riparato"
    assert i18n.label("status", "something_new", lang="it") == "something_new"


# ----------------------------------------------------------------------------- real commands


@pytest.fixture
def quota(tmp_path):
    path = tmp_path / "quota.py"
    path.write_text(QUOTA)
    return path


def test_verify_is_english_by_default_and_italian_with_lang(quota, tmp_path):
    runner = CliRunner()
    args = ["verify", str(quota), "--inprocess", "--ledger", str(tmp_path / "l.db")]
    en = runner.invoke(app, args)
    assert en.exit_code == 1
    assert "FALSIFIED" in en.stdout and "minimal input:" in en.stdout and "statement" in en.stdout
    it = runner.invoke(app, ["--lang", "it", *args])
    assert it.exit_code == 1
    assert "FALSIFICATO" in it.stdout and "input minimo:" in it.stdout and "enunciato" in it.stdout
    assert "minimal input" not in it.stdout
    # the choice does not leak into the next in-process invocation
    assert "FALSIFIED" in runner.invoke(app, args).stdout


def test_environment_variable_selects_italian(quota):
    runner = CliRunner()
    assert runner.invoke(app, ["audit", str(quota)]).stdout.startswith("ALLOW")
    out = runner.invoke(app, ["audit", str(quota)], env={i18n.ENV_VAR: "it"}).stdout
    assert out.startswith("CONSENTITO") and "segnalazioni: 0" in out
    out = runner.invoke(app, ["--lang", "en", "audit", str(quota)], env={i18n.ENV_VAR: "it"}).stdout
    assert out.startswith("ALLOW")  # --lang wins over the environment


def test_heal_in_italian(quota, tmp_path):
    r = CliRunner().invoke(app, ["--lang", "it", "heal", str(quota), "--inprocess", "--ledger", str(tmp_path / "l.db")])
    assert r.exit_code == 0, r.stdout
    assert "RIPARATO" in r.stdout and "patch verificata" in r.stdout
    assert "validazione indipendente: SUPERATA" in r.stdout


def test_json_output_is_never_translated(quota, tmp_path):
    args = ["verify", str(quota), "--json", "--inprocess", "--ledger", str(tmp_path / "l.db")]
    en = json.loads(CliRunner().invoke(app, args).stdout)
    it = json.loads(CliRunner().invoke(app, ["--lang", "it", *args]).stdout)
    assert en["status"] == it["status"] == "falsified"
    assert set(en) == set(it)
    assert [p["origin"] for p in it["targets"][0]["properties"]] == [
        p["origin"] for p in en["targets"][0]["properties"]
    ]


def test_help_follows_the_language():
    runner = CliRunner()
    assert "Run the CEGIS loop" in runner.invoke(app, ["heal", "--help"]).stdout
    it = runner.invoke(app, ["--lang", "it", "heal", "--help"]).stdout
    assert "Esegue il ciclo CEGIS" in it and "sovrascrive il file" in it
    assert "Mostra la versione" in runner.invoke(app, ["--help"], env={i18n.ENV_VAR: "it"}).stdout


def test_invalid_lang_is_rejected():
    r = CliRunner().invoke(app, ["--lang", "fr", "version"])
    assert r.exit_code == 2


def test_dashboard_in_italian(inprocess_config):
    import io

    from rich.console import Console

    from spec_forge.interfaces.tui import run_dashboard
    from spec_forge.orchestrator.cegis import SpecForge

    i18n.set_language("it")
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    with SpecForge(inprocess_config) as sf:
        rep = run_dashboard(QUOTA, forge=sf, console=console)
    out = console.file.getvalue()
    assert rep["status"] == "repaired"
    assert "patch verificata" in out and "log degli eventi" in out and "contratti dedotti" in out
