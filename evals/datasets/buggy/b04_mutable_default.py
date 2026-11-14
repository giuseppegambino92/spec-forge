"""Audit-event registry helper."""


def register_event(name: str, registry: list[str] = []) -> list[str]:
    """Return a registry containing all events of ``registry`` followed by ``name``.

    :ensures: result[-1] == name
    """
    registry.append(name)
    return registry
