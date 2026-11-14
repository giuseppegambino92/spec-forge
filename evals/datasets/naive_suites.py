"""Naive, happy-path unit tests of the kind that typically ship with each module.

Each statement is executed against the module namespace; a suite "catches" a bug if any
statement fails. By construction these tests exercise only typical inputs, mirroring the
example-based tests that let these bugs reach production.
"""

NAIVE_SUITES: dict[str, list[str]] = {
    "B01": ["assert split_payment(100, 2) == [50, 50]",
            "assert split_payment(90, 3) == [30, 30, 30]",
            "assert len(split_payment(1000, 4)) == 4"],
    "B02": ["assert admit_requests([0, 1, 2], 3, 10) == [True, True, True]",
            "assert admit_requests([0, 0, 0, 0], 3, 10) == [True, True, True, False]",
            "assert admit_requests([], 1, 1) == []"],
    "B03": ["assert normalize_whitespace('  hello  world ') == 'hello world'",
            "assert normalize_whitespace('a') == 'a'",
            "assert normalize_whitespace('') == ''"],
    "B04": ["assert register_event('login', []) == ['login']",
            "assert register_event('logout', ['login']) == ['login', 'logout']"],
    "B05": ["z = zscores([1.0, 2.0, 3.0]); assert abs(z[0] + 1.2247) < 1e-3 and abs(z[1]) < 1e-9",
            "assert len(zscores([4.0, 8.0])) == 2"],
    "B06": ["assert deduct_quota(100, 30) == 70",
            "assert deduct_quota(5, 0) == 5",
            "assert deduct_quota(10, 10) == 0"],
    "B07": ["assert ledger_total([10.0, -2.5, 4.5]) == 12.0",
            "assert ledger_total([]) == 0"],
    "B08": ["assert paginate(list(range(10)), 0, 3) == [0, 1, 2]",
            "assert paginate(list(range(10)), 1, 3) == [3, 4, 5]"],
    "B09": ["assert rle_decode([('a', 3), ('b', 1)]) == 'aaab'",
            "assert rle_encode('') == []"],
    "B10": ["assert top_k([3, 1, 2], 2) == [3, 2]",
            "assert top_k([], 3) == []"],
    "B11": ["c = HitCounter(); c.increment(); c.increment(); assert c.hits == 2",
            "c = HitCounter(); c.increment(5); assert c.hits == 5"],
    "B12": ["assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 3.0",
            "assert percentile([5.0], 0) == 5.0"],
}
