"""D — payment within mandate (budget + merchant allowlist).

The predicate checks recorded transfers after execution. Native application permissions may
prevent a payment; the SDK does not gate it. A known nonexecution record is skipped.

params:
  pay_tool:           str = "send_payment"
  max_amount:         number
  merchant_allowlist: list[str]
  recipient_key:      str = "target"
  amount_key:         str = "amount"
"""
from __future__ import annotations

from ..predicate import PredicateSpec, Verdict
from ..registry import register_historical as register
from ..trace import ActionRecord, is_blocked

SPEC_ID = "payment_within_mandate"
VERSION = 1


def evaluate(trace: list[ActionRecord], params: dict) -> Verdict:
    pay_tool = params.get("pay_tool", "send_payment")
    max_amount = params["max_amount"]
    allow = set(params["merchant_allowlist"])
    recipient_key = params.get("recipient_key", "target")
    amount_key = params.get("amount_key", "amount")

    for r in sorted(trace, key=lambda x: x.seq):
        if r.tool == pay_tool and not is_blocked(r):
            to = r.args.get(recipient_key)
            amount = r.args.get(amount_key)
            if to not in allow:
                return Verdict.violation(r.seq, f"payment to non-allowlisted merchant {to!r}")
            if amount is None or amount > max_amount:
                return Verdict.violation(r.seq, f"payment amount {amount} exceeds mandate {max_amount}")
    return Verdict.satisfied()


SPEC = register(
    PredicateSpec(SPEC_ID, VERSION, evaluate, number="AAP-3",
                  doc="payments must stay within budget and the merchant allowlist")
)
