"""Regenerate the README terminal screenshots (docs/images/{en,it}/*.svg) from real CLI runs.

Every image is the recorded output of an actual ``specforge`` invocation, parsed by the real
Typer app, on the bundled benchmark data under ``evals/datasets``. Nothing is typed by hand:
the module-level Rich console of the CLI is swapped for a ``Console(record=True)`` and the
recording is exported with ``export_svg``. Each command runs once with ``--lang en`` and once
with ``--lang it``.

No network and no model: the commands run the default offline engine (subprocess sandbox).
Each run uses a fresh scratch working directory, so the counterexample ledger starts empty and
the images do not depend on earlier runs. Timings in the images are whatever the run measured.

Usage (from the repository root)::

    python scripts/make_screenshots.py            # all screenshots, both languages
    python scripts/make_screenshots.py audit heal # only some of them
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from spec_forge.interfaces import cli

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
LANGS = ("en", "it")


@dataclass(frozen=True)
class Shot:
    name: str
    args: tuple[str, ...]  # CLI arguments after the global options; paths relative to the repo root
    exit_code: int  # expected exit status (the run must behave as documented)
    width: int = 100
    height: int | None = None  # only the dashboard needs a fixed height (it renders a Layout)


SHOTS = (
    Shot("verify", ("verify", "evals/datasets/buggy/b06_quota_underflow.py"), exit_code=1),
    Shot("heal", ("heal", "evals/datasets/buggy/b01_split_payment.py"), exit_code=0),
    Shot("audit", ("audit", "contract_injection.py"), exit_code=1),
    Shot("dash", ("dash", "evals/datasets/buggy/b02_sliding_window.py"), exit_code=0, width=150, height=44),
)


def _materialise_payload(workdir: Path, name: str) -> Path:
    """Write the bundled adversarial payload ``name`` (without .py) to ``workdir`` for ``specforge audit``."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))  # the `evals` package lives at the repository root, outside the wheel
    from evals.datasets.adversarial_payloads import PAYLOADS

    source = {pid: src for pid, _, src in PAYLOADS}[name]
    path = workdir / f"{name}.py"
    path.write_text(source.lstrip("\n"), encoding="utf-8")
    return path


def capture(shot: Shot, lang: str) -> str:
    console = Console(
        record=True,
        width=shot.width,
        height=shot.height,
        file=io.StringIO(),
        force_terminal=False,
        color_system="truecolor",
    )
    original_console, original_cwd = cli.console, Path.cwd()
    with tempfile.TemporaryDirectory(prefix="specforge-shot-") as tmp:
        workdir = Path(tmp)
        args = []
        for arg in shot.args:
            if arg.endswith(".py") and (ROOT / arg).exists():
                args.append(str(ROOT / arg))
            elif arg.endswith(".py"):
                args.append(str(_materialise_payload(workdir, arg[:-3])))
            else:
                args.append(arg)
        cli.console = console
        os.chdir(workdir)  # fresh default ledger (.specforge/ledger.db) for every run
        try:
            result = CliRunner().invoke(cli.app, ["--lang", lang, *args])
        finally:
            os.chdir(original_cwd)
            cli.console = original_console
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    if result.exit_code != shot.exit_code:
        raise SystemExit(
            f"{shot.name} [{lang}]: exit code {result.exit_code}, expected {shot.exit_code}\n{console.export_text()}"
        )
    title = " ".join(["specforge", *(["--lang", "it"] if lang == "it" else []), *shot.args])
    return console.export_svg(title=title)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("names", nargs="*", help=f"subset of: {', '.join(s.name for s in SHOTS)}")
    names = set(ap.parse_args(argv).names) or {s.name for s in SHOTS}
    unknown = names - {s.name for s in SHOTS}
    if unknown:
        ap.error(f"unknown screenshot(s): {', '.join(sorted(unknown))}")
    for shot in SHOTS:
        if shot.name not in names:
            continue
        for lang in LANGS:
            dest = OUT / lang / f"{shot.name}.svg"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(capture(shot, lang), encoding="utf-8")
            print(f"wrote {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
