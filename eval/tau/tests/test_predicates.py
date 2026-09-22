import pytest

from eval import predicates


def test_count_cap_default_skips_rejected():
    trace = [
        {"seq": 1, "tool": "GET /x", "args": {}},
        {"seq": 2, "tool": "GET /x", "args": {}},
    ]
    inputs = {
        "count_caps": [{"tools": ["GET /x"], "cap": 1, "arm": "cap"}],
        "rejected": {2},
    }
    assert predicates.run(trace, inputs) == []


def test_count_cap_attempt_mode_counts_rejected():
    trace = [
        {"seq": 1, "tool": "GET /x", "args": {}},
        {"seq": 2, "tool": "GET /x", "args": {}},
    ]
    inputs = {
        "count_caps": [
            {
                "tools": ["GET /x"],
                "cap": 1,
                "arm": "cap",
                "mode": "attempts",
            }
        ],
        "rejected": {2},
    }
    assert predicates.run(trace, inputs)[0]["seq"] == 2


def test_count_cap_rejects_unknown_mode():
    inputs = {
        "count_caps": [
            {"tools": ["GET /x"], "cap": 1, "arm": "cap", "mode": "attempt"}
        ]
    }

    with pytest.raises(ValueError, match="unknown count cap mode"):
        predicates.run([], inputs)
