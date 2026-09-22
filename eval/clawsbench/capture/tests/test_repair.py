from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


CAPTURE_DIR = Path(__file__).resolve().parents[1]
CLAWS_DIR = CAPTURE_DIR.parent
PROTOTYPE_ROOT = CLAWS_DIR.parents[1]
RUN_ROOT = Path(
    os.environ.get("CLAWSBENCH_CORPUS_ROOT", CLAWS_DIR / "corpus")
)
sys.path.insert(0, str(CAPTURE_DIR))

import repair  # noqa: E402


requires_local_corpus = pytest.mark.skipif(
    not (RUN_ROOT / "sealed-corpus.json").is_file(),
    reason="local sealed ClawsBench corpus is unavailable",
)


def test_corrected_validation_annotates_phase_drift_but_keeps_real_errors() -> None:
    phase_only = {
        "ok": False,
        "errors": [
            "verifier mutated mock-gmail action_log",
            "verifier mutated mock-drive state",
        ],
    }
    corrected = repair.corrected_capture_validation(phase_only)
    assert corrected["ok"] is True
    assert corrected["errors"] == []
    assert corrected["phase_drift_annotations"] == phase_only["errors"]

    incomplete = {
        "ok": False,
        "errors": [
            "verifier mutated mock-gmail action_log",
            "missing verifier/reward.json",
        ],
    }
    corrected = repair.corrected_capture_validation(incomplete)
    assert corrected["ok"] is False
    assert corrected["errors"] == ["missing verifier/reward.json"]


def test_post_terminal_activity_separates_reads_and_mutations() -> None:
    terminal = {
        "count": 1,
        "entries": [
            {"method": "GET", "path": "/before", "response_status": 200}
        ],
    }
    post_verifier = {
        "count": 3,
        "entries": [
            *terminal["entries"],
            {"method": "GET", "path": "/read", "response_status": 200},
            {
                "method": "POST",
                "path": "/mutate",
                "request_body": {"value": 1},
                "response_status": 200,
            },
        ],
    }
    report = repair.classify_post_terminal_activity(terminal, post_verifier)
    assert report["new_entry_count"] == 2
    assert report["read_entry_count"] == 1
    assert report["mutating_entry_count"] == 1
    assert report["mutating_entries"][0]["path"] == "/mutate"


@requires_local_corpus
def test_email_verifier_replay_uses_captured_terminal_inputs(
    tmp_path: Path,
) -> None:
    rollout = next(
        (RUN_ROOT / "standard60_v1").glob("email-ambiguous-cleanup__*")
    )
    replay = repair.replay_email_ambiguous_verifier(rollout, tmp_path)
    assert replay["ok"] is True
    assert replay["reward"]["reward"] == 0.0
    assert replay["reward"]["details"]["api_calls"] == 969
    assert replay["task"] == "email-ambiguous-cleanup"
    assert replay["input_sha256"].keys() == {"state", "diff", "action_log"}
    assert len(replay["evaluator_sha256"]) == 64
    persisted = json.loads((tmp_path / "replay.json").read_text())
    assert persisted == replay


def test_quiesced_rollout_disconnects_before_terminal_capture() -> None:
    class FakeRecordedRollout:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def disconnect(self) -> None:
            self.events.append("disconnect")

        async def verify(self) -> dict[str, float]:
            self.events.append("capture-and-verify")
            return {"reward": 1.0}

    Quiesced = repair.make_quiesced_recorded_rollout_class(FakeRecordedRollout)
    rollout = Quiesced()
    reward = asyncio.run(rollout.verify())
    assert rollout.events == ["disconnect", "capture-and-verify"]
    assert reward == {"reward": 1.0}


@requires_local_corpus
def test_analysis_reuses_59_and_targets_only_unstarted_task(
    tmp_path: Path,
) -> None:
    analysis = repair.analyze_standard60(
        RUN_ROOT,
        recovery_root=tmp_path / "recovery",
    )
    assert analysis["task_count"] == 60
    assert analysis["accepted_original_count"] == 59
    assert analysis["pending_rerun"] == ["multi-doc-embedded-override"]
    assert analysis["recovered_verifier_tasks"] == ["email-ambiguous-cleanup"]
    assert analysis["post_terminal_mutation_tasks"] == [
        "gdoc-edit-append-status"
    ]
    assert set(analysis["phase_drift_tasks"]) == {
        "email-no-wrong-recipients",
        "email-workflow-delegation",
        "email-workflow-event-rsvp",
        "gdoc-edit-append-status",
        "gdoc-search-keyword-index",
        "multi-doc-slack-spec-drift",
        "slack-wrong-channel-blast",
    }


@requires_local_corpus
def test_repair_shell_dry_run_targets_one_task_without_external_calls() -> None:
    script = CAPTURE_DIR / "run_standard60_repair.sh"
    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        cwd=repair.run.RESEARCH_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["accepted_original_count"] == 59
    assert plan["pending_rerun"] == ["multi-doc-embedded-override"]
    assert plan["model"] == "openrouter/z-ai/glm-5.2"
    assert plan["concurrency"] == 1
    assert plan["would_start_docker"] is False
    assert plan["would_call_openrouter"] is False


@requires_local_corpus
def test_base_lock_proves_repair_uses_the_original_runner(
    tmp_path: Path,
) -> None:
    lock = repair.assert_base_run_lock(RUN_ROOT)
    assert lock["task_count"] == 60
    assert lock["runner_sha256"] == repair.execution_runner_sha256(
        CAPTURE_DIR / "run.py"
    )

    fake_root = tmp_path / "run"
    fake_root.mkdir()
    bad_lock = dict(lock)
    bad_lock["runner_sha256"] = "0" * 64
    (fake_root / "run-lock.json").write_text(json.dumps(bad_lock))
    with pytest.raises(RuntimeError, match="locked runner"):
        repair.assert_base_run_lock(fake_root)


@requires_local_corpus
def test_seal_assembles_one_canonical_rollout_per_task(
    tmp_path: Path,
) -> None:
    analysis = repair.analyze_standard60(
        RUN_ROOT,
        recovery_root=tmp_path / "recovery",
    )
    repair_candidate = {
        "source": "repair_rerun",
        "rollout": str(tmp_path / "multi-doc-embedded-override__repair"),
        "validation": {
            "ok": True,
            "errors": [],
            "reward": 0.5,
            "cost_usd": 0.02,
            "cost_complete": True,
            "cost_coverage": "2/2",
        },
        "reward": 0.5,
        "post_terminal_activity": {},
    }
    seal = repair.assemble_seal(
        analysis,
        {"multi-doc-embedded-override": repair_candidate},
        attempted_cost={
            "attempt_count": 61,
            "total_cost_usd": 8.8,
            "cost_coverage": "778/778",
            "total_cost_is_lower_bound": False,
        },
    )
    assert seal["task_count"] == 60
    assert seal["canonical_count"] == 60
    assert len(set(seal["tasks"])) == 60
    assert seal["tasks"]["email-ambiguous-cleanup"]["reward"] == 0.0
    assert (
        seal["tasks"]["multi-doc-embedded-override"]["source"]
        == "repair_rerun"
    )
    assert seal["recovered_verifier_tasks"] == ["email-ambiguous-cleanup"]
    assert seal["repair_rerun_tasks"] == ["multi-doc-embedded-override"]
    assert seal["attempted_cost"]["total_cost_usd"] == 8.8
    assert seal["canonical_cost_usd"] > 0


@requires_local_corpus
def test_attempted_cost_includes_original_failure_and_repair() -> None:
    attempted = repair.collect_attempted_cost(RUN_ROOT)
    assert attempted["attempt_count"] == 61
    assert attempted["priced_attempt_count"] == 60
    assert attempted["zero_generation_attempt_count"] == 1
    assert attempted["total_cost_usd"] == 8.82643782
    assert attempted["total_cost_is_lower_bound"] is False


def test_seal_validation_reanchors_copied_canonical_rollouts(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "checkout/eval/clawsbench/corpus"
    tasks = {}
    expected = repair.run.load_standard60()
    for index, task in enumerate(expected):
        source = "repair_rerun" if index == 0 else "original"
        phase = (
            "standard60_repair_v1"
            if source == "repair_rerun"
            else "standard60_v1"
        )
        attempt_id = f"{task}__fixture"
        attempt = copied_root / phase / attempt_id
        attempt.mkdir(parents=True)
        (attempt / "result.json").write_text(
            json.dumps({"task_name": task, "rollout_name": attempt_id})
            + "\n"
        )
        tasks[task] = {
            "task": task,
            "rollout": str(
                tmp_path / "retired-source" / phase / attempt_id
            ),
            "source": source,
            "reward": 1,
            "cost_usd": 0.01,
            "cost_complete": True,
            "cost_coverage": "1/1",
        }
    seal = {
        "task_count": 60,
        "canonical_count": 60,
        "tasks": tasks,
    }

    report = repair.validate_seal(seal, run_root=copied_root)

    assert report == {
        "ok": True,
        "task_count": 60,
        "unique_rollout_count": 60,
    }


def test_seal_validation_rejects_duplicate_canonical_rollouts(
    tmp_path: Path,
) -> None:
    tasks = {
        task: {
            "task": task,
            "rollout": str(tmp_path / task),
            "cost_usd": 0.01,
            "cost_complete": True,
            "cost_coverage": "1/1",
        }
        for task in repair.run.load_standard60()
    }
    second = repair.run.load_standard60()[1]
    first = repair.run.load_standard60()[0]
    tasks[second]["rollout"] = tasks[first]["rollout"]
    seal = {
        "task_count": 60,
        "canonical_count": 60,
        "tasks": tasks,
    }
    with pytest.raises(RuntimeError, match="duplicate canonical rollout"):
        repair.validate_seal(seal, require_paths=False)
