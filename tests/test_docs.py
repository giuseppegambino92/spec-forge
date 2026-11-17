"""Documentation consistency: README.md (English) and README.it.md (Italian) must stay in lockstep."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EN = ROOT / "README.md"
IT = ROOT / "README.it.md"

FENCE = re.compile(r"^```[^\n]*\n.*?^```", re.S | re.M)
MATH_FENCE = re.compile(r"^```math\n(.*?)^```", re.S | re.M)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
VERSION = re.compile(r"\b(?:macOS|Python|JSON-RPC)\s+\d+(?:\.\d+)+|\d+(?:\.\d+){2,}")  # versions, not measurements
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
URL = re.compile(r"https?://[^\s)\]]+")  # badge and link targets are not prose
NUM = {
    "en": re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"),
    "it": re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?"),
}


def _read(p: Path) -> str:
    if not p.exists():
        pytest.fail(f"{p.name} is missing")
    return p.read_text(encoding="utf-8")


def numbers(text: str, lang: str) -> Counter:
    prose = INLINE_CODE.sub(" ", FENCE.sub(" ", text))
    prose = DATE.sub(" ", VERSION.sub(" ", URL.sub(" ", prose)))
    out: Counter = Counter()
    for tok in NUM[lang].findall(prose):
        if lang == "en":
            tok = tok.replace(",", "")
        else:
            tok = tok.replace(".", "").replace(",", ".")
        out[float(tok)] += 1
    return out


def text_blocks_with_underscore(text: str) -> list[str]:
    """Return every \\text{...} body that contains an underscore not escaped as \\_."""
    bad = []
    for m in re.finditer(r"\\text\{", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        body = text[m.end(): i - 1]
        if re.search(r"(?<!\\)_", body):
            bad.append(body)
    return bad


def test_cross_links_at_the_top():
    en, it = _read(EN), _read(IT)
    assert "[Italiano](README.it.md)" in "\n".join(en.splitlines()[:5])
    assert "[English](README.md)" in "\n".join(it.splitlines()[:5])


def test_math_blocks_are_identical():
    en, it = _read(EN), _read(IT)
    assert MATH_FENCE.findall(en), "expected the Builder formulas in README.md"
    assert MATH_FENCE.findall(en) == MATH_FENCE.findall(it)


@pytest.mark.parametrize("path", [EN, IT], ids=["README.md", "README.it.md"])
def test_no_unescaped_underscores_inside_text_macros(path):
    assert text_blocks_with_underscore(_read(path)) == []


def test_text_macro_checker_detects_violations():
    assert text_blocks_with_underscore(r"$c_{\text{c_puct}}$") == ["c_puct"]
    assert text_blocks_with_underscore(r"$c_{\text{puct}} + \text{a\_b}$") == []


def test_inline_code_is_preserved_verbatim():
    assert Counter(INLINE_CODE.findall(FENCE.sub(" ", _read(EN)))) == \
        Counter(INLINE_CODE.findall(FENCE.sub(" ", _read(IT))))


def test_every_number_survives_translation():
    en, it = numbers(_read(EN), "en"), numbers(_read(IT), "it")
    missing, extra = en - it, it - en
    assert not missing and not extra, f"only in English: {dict(missing)}; only in Italian: {dict(extra)}"
