from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # make the `evals` package importable

from spec_forge.config import SpecForgeConfig  # noqa: E402


@pytest.fixture
def inprocess_config(tmp_path) -> SpecForgeConfig:
    cfg = SpecForgeConfig(ledger_path=str(tmp_path / "ledger.db"))
    cfg.sandbox.isolation = "inprocess"
    return cfg


@pytest.fixture
def subprocess_config(tmp_path) -> SpecForgeConfig:
    return SpecForgeConfig(ledger_path=str(tmp_path / "ledger.db"))


@pytest.fixture(scope="session")
def cases():
    from evals.datasets import CASES

    return CASES
