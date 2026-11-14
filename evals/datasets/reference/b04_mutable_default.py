"""Audit-event registry helper."""

from typing import Optional


def register_event(name: str, registry: Optional[list[str]] = None) -> list[str]:
    """Return a registry containing all events of ``registry`` followed by ``name``.

    :ensures: result[-1] == name
    """
    return [*(registry or []), name]
