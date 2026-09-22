"""Bundle builder tests: step structure, pairing, blinding, determinism."""

import json
import subprocess
from pathlib import Path

import pytest

from eval.labeling import build_tau_bundle
from eval.labeling.segment_policy import segment_policy

REPO_ROOT = Path(__file__).resolve().parents[3]
BANNED = [
    "reward",
    "glm",
    "qwen",
    "fire",
    "native",
    "model",
    "usage",
    "raw_data",
    "cost",
]

POLICY = """# Domain Policy

## Refunds

Confirm the order id with the user. Ask again if it does not match.

- Refuse duplicate refunds.
- Escalate anything unclear.
"""

LONG_RESULT = "x" * 1200


def _fixture_payload():
    messages = [
        {
            "role": "user",
            "content": "Hi, I need help with order O1.",
            "raw_data": {"provider": "glm-4.7-flash"},
            "usage": {"tokens": 5},
            "cost": 0.01,
        },
        {
            "role": "assistant",
            "content": "Let me look that up.",
            "raw_data": {"provider": "qwen3-30b"},
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "get_order",
                    "arguments": {"order_id": "O1"},
                    "requestor": "assistant",
                },
                {
                    "id": "call_2",
                    "name": "get_history",
                    "arguments": {"order_id": "O1"},
                    "requestor": "assistant",
                },
            ],
        },
        {"role": "tool", "id": "call_1", "content": LONG_RESULT, "error": False},
        {"role": "tool", "id": "call_2", "content": "not found", "error": True},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_3",
                    "name": "return_order",
                    "arguments": {"order_id": "O1"},
                    "requestor": "assistant",
                }
            ],
        },
        {"role": "tool", "id": "call_3", "content": "ok", "error": False},
        {"role": "assistant", "content": "All set."},
    ]
    simulation = {
        "id": "sim-1",
        "task_id": "7",
        "trial": 0,
        "policy": POLICY,
        "messages": messages,
        "reward_info": {"reward": 1.0, "db_check": {"db_match": True}},
        "agent_cost": 0.5,
        "raw_data": {"fired": "never"},
    }
    task = {
        "id": "7",
        "user_scenario": {
            "persona": None,
            "instructions": {
                "domain": "retail",
                "reason_for_call": "You want to return order O1.",
                "known_info": "You are Pat Doe.",
                "unknown_info": None,
                "task_instructions": "Stay polite.",
            },
        },
    }
    return {"simulations": [simulation], "tasks": [task], "info": {"checkpoint": "qwen3-30b"}}


@pytest.fixture()
def fixture_corpus(tmp_path):
    shard = tmp_path / "corpus" / "accepted" / "tau" / "m" / "retail"
    shard.mkdir(parents=True)
    (shard / "results.json").write_text(json.dumps(_fixture_payload()))
    selection = {
        "tau": {
            "runs": [
                {
                    "display_id": "run-01",
                    "domain": "retail",
                    "task_id": "7",
                    "trial": 0,
                    "source": {
                        "path": "corpus/accepted/tau/m/retail/results.json",
                        "root_sha256": "0" * 64,
                    },
                }
            ]
        }
    }
    return selection, tmp_path


def test_step_ordering_and_pairing(fixture_corpus):
    selection, corpus_root = fixture_corpus
    bundle = build_tau_bundle.build_bundle(selection, corpus_root)
    manifest = json.loads(bundle["tasks/run-01.json"])
    steps = manifest["steps"]
    assert [step["index"] for step in steps] == list(range(len(steps)))
    assert [step["type"] for step in steps] == [
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_call",
        "tool_call",
        "assistant_message",
    ]
    calls = [step for step in steps if step["type"] == "tool_call"]
    assert [call["tool_call_id"] for call in calls] == ["call_1", "call_2", "call_3"]
    assert calls[0]["tool"] == "get_order"
    assert calls[0]["args"] == {"order_id": "O1"}
    assert calls[0]["result"] == LONG_RESULT
    assert calls[0]["result_truncated"] is False
    assert calls[0]["error"] is False
    assert calls[1]["result"] == "not found"
    assert calls[1]["result_truncated"] is False
    assert calls[1]["error"] is True
    assert calls[2]["result"] == "ok"


def test_manifest_shape_and_index(fixture_corpus):
    selection, corpus_root = fixture_corpus
    bundle = build_tau_bundle.build_bundle(selection, corpus_root)
    manifest = json.loads(bundle["tasks/run-01.json"])
    assert manifest["run"] == "run-01"
    assert manifest["benchmark"] == "tau"
    assert manifest["schema_version"] == 1
    assert manifest["domain"] == "retail"
    assert "You want to return order O1." in manifest["user_scenario"]
    assert "You are Pat Doe." in manifest["user_scenario"]
    assert "Stay polite." in manifest["user_scenario"]
    index = json.loads(bundle["index.json"])
    assert index["selection"]["count"] == 1
    assert index["domains"] == ["retail"]
    assert index["runs"] == [
        {"run": "run-01", "domain": "retail", "steps": 6, "tool_calls": 3}
    ]


def test_segment_ids_stable():
    segments = segment_policy(POLICY, "retail")
    assert [segment["id"] for segment in segments] == [
        "retail-p01-s01",
        "retail-p01-s02",
        "retail-p02-s03",
        "retail-p02-s04",
    ]
    assert [segment["text"] for segment in segments] == [
        "Confirm the order id with the user.",
        "Ask again if it does not match.",
        "- Refuse duplicate refunds.",
        "- Escalate anything unclear.",
    ]
    assert {segment["heading"] for segment in segments} == {"Refunds"}
    assert segment_policy(POLICY, "retail") == segments


def test_missing_tool_result_raises(fixture_corpus):
    selection, corpus_root = fixture_corpus
    payload = _fixture_payload()
    del payload["simulations"][0]["messages"][3]
    path = corpus_root / "corpus" / "accepted" / "tau" / "m" / "retail" / "results.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="no result"):
        build_tau_bundle.build_bundle(selection, corpus_root)


def test_blinding(fixture_corpus):
    selection, corpus_root = fixture_corpus
    bundle = build_tau_bundle.build_bundle(selection, corpus_root)
    blob = "".join(bundle.values()).lower()
    for token in BANNED:
        assert token not in blob, token


def test_determinism(fixture_corpus, tmp_path):
    selection, corpus_root = fixture_corpus
    first = build_tau_bundle.build_bundle(selection, corpus_root)
    second = build_tau_bundle.build_bundle(selection, corpus_root)
    assert first == second
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    build_tau_bundle.write_bundle(first, out_a)
    build_tau_bundle.write_bundle(second, out_b)
    for relative in first:
        assert (out_a / relative).read_bytes() == (out_b / relative).read_bytes()
        assert (out_a / relative).read_bytes().endswith(b"\n")


def _tool_calls(steps):
    return [step for step in steps if step["type"] == "tool_call"]


def _out_dir_state():
    """Tracked-bundle fingerprint: git's view plus per-file size and mtime."""

    status = subprocess.run(
        ["git", "status", "--short", "eval/viewer"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = {
        str(path.relative_to(build_tau_bundle.OUT_DIR)): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(build_tau_bundle.OUT_DIR.rglob("*.json"))
    }
    return status, files


def test_result_limit_truncates_only_when_set():
    messages = _fixture_payload()["simulations"][0]["messages"]

    limited = _tool_calls(build_tau_bundle.build_steps(messages, result_limit=1000))
    assert limited[0]["result"] == LONG_RESULT[:1000]
    assert limited[0]["result_truncated"] is True
    assert limited[1]["result"] == "not found"
    assert limited[1]["result_truncated"] is False

    default = _tool_calls(build_tau_bundle.build_steps(messages))
    assert default[0]["result"] == LONG_RESULT
    assert default[0]["result_truncated"] is False

    wide = _tool_calls(build_tau_bundle.build_steps(messages, result_limit=5000))
    assert wide[0]["result"] == LONG_RESULT
    assert wide[0]["result_truncated"] is False


def test_bundle_kwargs_thread_through(fixture_corpus):
    selection, corpus_root = fixture_corpus
    bundle = build_tau_bundle.build_bundle(
        selection,
        corpus_root,
        result_limit=1000,
        label="Tau extension set",
        selection_file="eval/labeling/extension/selection.json",
    )
    index = json.loads(bundle["index.json"])
    assert index["label"] == "Tau extension set"
    assert index["selection"]["file"] == "eval/labeling/extension/selection.json"
    calls = _tool_calls(json.loads(bundle["tasks/run-01.json"])["steps"])
    assert calls[0]["result"] == LONG_RESULT[:1000]
    assert calls[0]["result_truncated"] is True
    assert calls[1]["result_truncated"] is False

    default = json.loads(
        build_tau_bundle.build_bundle(selection, corpus_root)["index.json"]
    )
    assert default["label"] == "Tau labeling set"
    assert default["selection"]["file"] == "eval/labeling/selection_v1.json"


def test_main_refuses_to_prune_calibration_dir(tmp_path, capsys):
    selection_path = tmp_path / "selection.json"
    selection_path.write_text("{")  # unparseable: the guard must fire before any read
    before = _out_dir_state()
    code = build_tau_bundle.main(
        ["--selection", str(selection_path), "--out", str(build_tau_bundle.OUT_DIR)]
    )
    assert code == 2
    assert "refusing" in capsys.readouterr().err
    assert _out_dir_state() == before


def test_main_result_limit_parsing(fixture_corpus, tmp_path, capsys):
    selection, corpus_root = fixture_corpus
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selection))

    def run(out_name, limit):
        out = tmp_path / out_name
        code = build_tau_bundle.main(
            [
                "--selection",
                str(selection_path),
                "--corpus-root",
                str(corpus_root),
                "--out",
                str(out),
                "--result-limit",
                limit,
            ]
        )
        assert code == 0
        return out, capsys.readouterr().out

    out, printed = run("out_none", "none")
    assert "result_limit=None" in printed
    calls = _tool_calls(json.loads((out / "tasks" / "run-01.json").read_text())["steps"])
    assert calls[0]["result"] == LONG_RESULT
    assert calls[0]["result_truncated"] is False
    # a selection outside the repo falls back to its plain path in index.json
    index = json.loads((out / "index.json").read_text())
    assert index["selection"]["file"] == str(selection_path)

    out, printed = run("out_empty", "")
    assert "result_limit=None" in printed
    calls = _tool_calls(json.loads((out / "tasks" / "run-01.json").read_text())["steps"])
    assert calls[0]["result_truncated"] is False

    out, printed = run("out_cut", "1000")
    assert "result_limit=1000" in printed
    calls = _tool_calls(json.loads((out / "tasks" / "run-01.json").read_text())["steps"])
    assert calls[0]["result"] == LONG_RESULT[:1000]
    assert calls[0]["result_truncated"] is True
    assert calls[1]["result_truncated"] is False


@pytest.mark.skipif(
    not (REPO_ROOT / "eval" / "paper_main_v1" / "corpus" / "accepted" / "tau").exists(),
    reason="sealed Tau corpus not present",
)
def test_real_corpus_bundle():
    selection = json.loads(
        (REPO_ROOT / "eval" / "labeling" / "selection_v1.json").read_text()
    )
    bundle = build_tau_bundle.build_bundle(
        selection, REPO_ROOT / "eval" / "paper_main_v1"
    )
    assert len(bundle) == 31
    index = json.loads(bundle["index.json"])
    assert index["selection"]["count"] == 30
    assert [row["run"] for row in index["runs"]] == [
        f"run-{i:02d}" for i in range(1, 31)
    ]
    total = sum(len(text.encode("utf-8")) for text in bundle.values())
    assert total < 5 * 1024 * 1024
    blob = "".join(bundle.values()).lower()
    for token in ("reward", "glm", "qwen", "fire"):
        assert token not in blob, token
