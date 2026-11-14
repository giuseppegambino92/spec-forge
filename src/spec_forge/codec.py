"""Value codec shared by the parent process and the sandbox worker.

Counterexamples cross a process boundary and are persisted in SQLite, so Python values
(tuples, sets, NaN/Inf floats, bytes, Decimals ...) must round-trip losslessly through JSON.
Also hosts semantic equality (NaN-aware) and the complexity metric used by the shrinker.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from decimal import Decimal
from fractions import Fraction
from typing import Any

_TAG = "__sf__"


def encode_value(v: Any) -> Any:
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isfinite(v):
            return v
        return {_TAG: "float", "v": repr(v)}
    if isinstance(v, list):
        return [encode_value(x) for x in v]
    if isinstance(v, tuple):
        return {_TAG: "tuple", "v": [encode_value(x) for x in v]}
    if isinstance(v, (set, frozenset)):
        items = sorted((encode_value(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
        return {_TAG: "frozenset" if isinstance(v, frozenset) else "set", "v": items}
    if isinstance(v, dict):
        if all(isinstance(k, str) for k in v) and _TAG not in v:
            return {k: encode_value(x) for k, x in v.items()}
        return {_TAG: "dict", "v": [[encode_value(k), encode_value(x)] for k, x in v.items()]}
    if isinstance(v, bytes):
        return {_TAG: "bytes", "v": base64.b64encode(v).decode()}
    if isinstance(v, Decimal):
        return {_TAG: "decimal", "v": str(v)}
    if isinstance(v, Fraction):
        return {_TAG: "fraction", "v": [v.numerator, v.denominator]}
    return {_TAG: "repr", "v": safe_repr(v)}


def decode_value(v: Any) -> Any:
    if isinstance(v, list):
        return [decode_value(x) for x in v]
    if isinstance(v, dict):
        tag = v.get(_TAG)
        if tag is None:
            return {k: decode_value(x) for k, x in v.items()}
        p = v["v"]
        if tag == "float":
            return float(p)
        if tag == "tuple":
            return tuple(decode_value(x) for x in p)
        if tag == "set":
            return {decode_value(x) for x in p}
        if tag == "frozenset":
            return frozenset(decode_value(x) for x in p)
        if tag == "dict":
            return {decode_value(k): decode_value(x) for k, x in p}
        if tag == "bytes":
            return base64.b64decode(p)
        if tag == "decimal":
            return Decimal(p)
        if tag == "fraction":
            return Fraction(p[0], p[1])
        if tag == "repr":
            return p
    return v


def dumps(obj: Any) -> str:
    return json.dumps(encode_value(obj), sort_keys=True, separators=(",", ":"))


def loads(s: str) -> Any:
    return decode_value(json.loads(s))


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(dumps(obj).encode()).hexdigest()[:16]


def sem_eq(a: Any, b: Any) -> bool:
    """Structural equality where NaN == NaN (so properties are not falsified by NaN identity)."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if type(a) is not type(b) or len(a) != len(b):
            return False
        return all(sem_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        return all(sem_eq(a[k], b[k]) for k in a)
    try:
        return bool(a == b)
    except Exception:
        return False


def complexity(v: Any) -> int:
    """Structural size of a value; the shrinker minimises this, benchmarks report its ratio.

    int: 1 + bit length | float: 1 + significant repr chars | str: 1 + len + non-ASCII chars
    containers: 1 + sum of children.
    """
    if v is None or isinstance(v, bool):
        return 1
    if isinstance(v, int):
        return 1 + abs(v).bit_length()
    if isinstance(v, float):
        if not math.isfinite(v):
            return 2
        r = repr(v).lstrip("-").replace("0.", "").rstrip("0").replace(".", "")
        return 1 + len(r.lstrip("0") or "")
    if isinstance(v, str):
        return 1 + len(v) + sum(1 for c in v if ord(c) > 127)
    if isinstance(v, bytes):
        return 1 + len(v)
    if isinstance(v, (list, tuple, set, frozenset)):
        return 1 + sum(complexity(x) for x in v)
    if isinstance(v, dict):
        return 1 + sum(complexity(k) + complexity(x) for k, x in v.items())
    return 1 + len(safe_repr(v))


def case_complexity(case: dict[str, Any]) -> int:
    """Complexity of an input case: sum over argument *values* (parameter names are constant overhead)."""
    return sum(complexity(v) for v in case.values())


def safe_repr(v: Any, limit: int = 160) -> str:
    try:
        r = repr(v)
    except Exception as exc:  # pragma: no cover - exotic objects
        r = f"<unreprable {type(v).__name__}: {exc}>"
    return r if len(r) <= limit else r[: limit - 3] + "..."


def format_call(name: str, case: dict[str, Any], limit: int = 240) -> str:
    args = ", ".join(f"{k}={safe_repr(v, 80)}" for k, v in case.items())
    s = f"{name}({args})"
    return s if len(s) <= limit else s[: limit - 3] + "..."
