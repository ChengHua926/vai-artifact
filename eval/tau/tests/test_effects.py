from __future__ import annotations

from eval.tau.effects import attribute_terminal_effects, logical_diff


def test_logical_diff_treats_lists_as_atomic_values() -> None:
    before = {"agent": {"orders": {"#W1": {"items": [{"item_id": "old"}]}}}}
    after = {"agent": {"orders": {"#W1": {"items": [{"item_id": "new"}]}}}}

    assert logical_diff(before, after) == [
        {
            "path": ["agent", "orders", "#W1", "items"],
            "before": [{"item_id": "old"}],
            "after": [{"item_id": "new"}],
            "before_present": True,
            "after_present": True,
        }
    ]


def test_terminal_effect_uses_last_writer_and_keeps_omission_unwritten() -> None:
    gold = {"agent": {"orders": {"#W1": {"status": "cancelled", "cancel_reason": "x"}}}}
    actual = {"agent": {"orders": {"#W1": {"status": "pending", "cancel_reason": None}}}}
    deltas = [
        {
            "seq": 2,
            "tool": "cancel_pending_order",
            "changes": [
                {
                    "path": ["agent", "orders", "#W1", "status"],
                    "before": "pending",
                    "after": "cancelled",
                    "before_present": True,
                    "after_present": True,
                }
            ],
        },
        {
            "seq": 3,
            "tool": "modify_pending_order_address",
            "changes": [
                {
                    "path": ["agent", "orders", "#W1", "status"],
                    "before": "cancelled",
                    "after": "pending",
                    "before_present": True,
                    "after_present": True,
                }
            ],
        },
    ]

    effects = attribute_terminal_effects(gold, actual, deltas)

    assert effects == [
        {
            "path": ["agent", "orders", "#W1", "cancel_reason"],
            "gold": "x",
            "actual": None,
            "gold_present": True,
            "actual_present": True,
            "writer_seq": None,
            "writer_tool": None,
        },
        {
            "path": ["agent", "orders", "#W1", "status"],
            "gold": "cancelled",
            "actual": "pending",
            "gold_present": True,
            "actual_present": True,
            "writer_seq": 3,
            "writer_tool": "modify_pending_order_address",
        },
    ]
