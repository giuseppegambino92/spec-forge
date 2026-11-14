"""spec-forge: neuro-symbolic multi-agent code verification & self-healing synthesis (CEGIS + MCTS)."""

from __future__ import annotations

__version__ = "1.0.0"

from .config import SpecForgeConfig  # noqa: E402
from .orchestrator.cegis import SpecForge  # noqa: E402
from .sandbox.firewall import audit_source  # noqa: E402

__all__ = ["SpecForge", "SpecForgeConfig", "audit_source", "__version__"]
