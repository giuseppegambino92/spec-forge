"""Text canonicalisation for search indexing."""


def normalize_whitespace(text: str) -> str:
    """Collapse runs of spaces to a single space and trim the ends.

    :ensures: "  " not in result
    """
    return text.replace("  ", " ").strip()
