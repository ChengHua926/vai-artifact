from __future__ import annotations

import json
import subprocess
import sys

from eval.tau.monitor import compiled_monitor_manifest, run_compiled_monitor


def test_compiled_monitor_keeps_existing_auth_and_status_fires() -> None:
    trace = [
        {
            "seq": 1,
            "tool": "get_order_details",
            "args": {"order_id": "#W1"},
            "result": '{"order_id":"#W1","user_id":"u_1","status":"delivered"}',
        },
        {
            "seq": 2,
            "tool": "cancel_pending_order",
            "args": {"order_id": "#W1", "reason": "ordered by mistake"},
            "result": "Error: Non-pending order cannot be cancelled",
        },
    ]
    turns = [{"role": "assistant", "text": "", "calls": [1, 2]}]

    result = run_compiled_monitor("retail", trace, turns)

    # Existing effects-only monitor skips the rejected second call, but fires on the
    # successful user-scoped read before authentication.
    assert result["fires"] == [
        {
            "seq": 1,
            "tool": "get_order_details",
            "predicate": "scope",
            "arm": "auth_first",
            "targets": ["get_order_details"],
            "detail": "user-scoped call before any successful find_user_id_*",
        }
    ]
    assert result["supplied_checks_by_seq"] == {"1": ["auth_first"]}
    assert result["rejected_seqs"] == [2]


def test_compiled_monitor_count_fire_stays_on_second_effectful_call() -> None:
    trace = [
        {
            "seq": 1,
            "tool": "modify_pending_order_items",
            "args": {
                "order_id": "#W1",
                "item_ids": ["old1"],
                "new_item_ids": ["new1"],
                "payment_method_id": "card1",
            },
            "result": "ok",
        },
        {
            "seq": 2,
            "tool": "exchange_delivered_order_items",
            "args": {
                "order_id": "#W1",
                "item_ids": ["old2"],
                "new_item_ids": ["new2"],
                "payment_method_id": "card1",
            },
            "result": "ok",
        },
    ]

    result = run_compiled_monitor(
        "retail", trace, [{"role": "assistant", "text": "", "calls": [1, 2]}]
    )

    assert [fire for fire in result["fires"] if fire["predicate"] == "count"] == [
        {
            "seq": 2,
            "tool": "exchange_delivered_order_items",
            "predicate": "count",
            "arm": "once_per_order",
            "targets": ["#W1"],
            "detail": "call 2 > cap 1",
        }
    ]


def test_compiled_monitor_is_named_and_source_hashed() -> None:
    manifest = compiled_monitor_manifest()

    assert manifest["implementation"] == "compiled_tau_monitor"
    assert {row["path"] for row in manifest["sources"]} == {
        "eval/predicates.py",
        "eval/tau/adapter.py",
        "eval/tau/promises.py",
    }
    assert all(len(row["sha256"]) == 64 for row in manifest["sources"])


def _subprocess_monitor_result(*, import_claws_first: bool) -> dict:
    poison = (
        "import importlib; "
        "importlib.import_module('eval.clawsbench.analysis.adapter'); "
        if import_claws_first
        else ""
    )
    script = poison + """
import json
from eval.tau.monitor import compiled_monitor_manifest, run_compiled_monitor
trace = [{
    "seq": 1,
    "tool": "get_order_details",
    "args": {"order_id": "#W1"},
    "result": '{"order_id":"#W1","user_id":"u_1","status":"delivered"}',
}]
turns = [{"role": "assistant", "text": "", "calls": [1]}]
print(json.dumps({
    "manifest": compiled_monitor_manifest(),
    "result": run_compiled_monitor("retail", trace, turns),
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_compiled_monitor_is_hermetic_to_claws_import_order() -> None:
    clean = _subprocess_monitor_result(import_claws_first=False)
    claws_first = _subprocess_monitor_result(import_claws_first=True)

    assert claws_first == clean


def test_compiled_monitor_does_not_poison_later_claws_import() -> None:
    script = """
import importlib
import json
import sys
from pathlib import Path
from eval.tau.monitor import run_compiled_monitor
trace = [{
    "seq": 1,
    "tool": "get_order_details",
    "args": {"order_id": "#W1"},
    "result": '{"order_id":"#W1","user_id":"u_1","status":"delivered"}',
}]
turns = [{"role": "assistant", "text": "", "calls": [1]}]
run_compiled_monitor("retail", trace, turns)
claws = importlib.import_module("eval.clawsbench.analysis.adapter")
print(json.dumps({
    "has_canonical_signature": hasattr(claws.promises, "canonical_signature"),
    "promises_path": Path(claws.promises.__file__).as_posix(),
    "tau_package_on_path": str(Path("eval/tau").resolve()) in sys.path,
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["has_canonical_signature"] is True
    assert result["promises_path"].endswith(
        "eval/clawsbench/analysis/promises.py"
    )
    assert result["tau_package_on_path"] is False
