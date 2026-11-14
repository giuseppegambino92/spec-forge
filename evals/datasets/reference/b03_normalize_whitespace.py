"""Text canonicalisation for search indexing."""

import re


def normalize_whitespace(text: str) -> str:
    """Collapse runs of spaces to a single space and trim the ends.

    :ensures: "  " not in result
    """
    return re.sub(r" {2,}", " ", text).strip()
