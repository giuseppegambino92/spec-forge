"""Cursor-less pagination for the admin listing endpoint."""


def paginate(items: list[int], page: int, size: int) -> list[int]:
    """Return the ``page``-th (0-based) page of ``items``.

    :requires: 1 <= size <= 10
    :requires: 0 <= page <= 20
    :ensures: len(result) <= size
    :law: [x for p in range(len(items) // size + 1) for x in paginate(items, p, size)] == items
    """
    start = page * size
    return items[start:start + size]
