from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ANALYSIS_DIR = Path(__file__).resolve().parents[1]
CLAWS_DIR = ANALYSIS_DIR.parent
PROTOTYPE_ROOT = CLAWS_DIR.parents[1]
VIEWER_APP = PROTOTYPE_ROOT / "eval/viewer/app"
REAL_RUN_ROOT = Path(
    os.environ.get("CLAWSBENCH_CORPUS_ROOT", CLAWS_DIR / "corpus")
)

from eval.clawsbench.analysis import viewer_export


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def _make_attempt(root: Path, task: str, suffix: str) -> Path:
    attempt = root / f"{task}__{suffix}"
    _json(attempt / "prompts.json", [f"prompt for {task}"])
    _json(
        attempt / "result.json",
        {
            "task_name": task,
            "rollout_name": attempt.name,
            "model": "openrouter/z-ai/glm-5.2",
            "skill_mode": "no-skill",
            "rewards": {
                "reward": 0.5,
                "metrics": {"done": 1},
                "details": {"note": "partial"},
            },
            "agent_result": {"total_tokens": 123},
        },
    )
    _json(attempt / "config.json", {"agent": "openclaw"})
    _json(attempt / "timing.json", {"total": 12.3})
    _json(attempt / "capture-validation.json", {"ok": True})
    timeline = attempt / "trajectory/acp_trajectory.jsonl"
    timeline.parent.mkdir(parents=True)
    timeline.write_text(
        '{"type":"user_message","text":"hello"}\n'
        "not-json\n"
        '{"type":"tool_call","kind":"exec","title":"echo hi"}\n'
    )
    llm = attempt / "trajectory/llm_trajectory.jsonl"
    llm.write_text(
        json.dumps(
            {
                "request": {
                    "body": {
                        "model": "glm",
                        "messages": [
                            {
                                "role": "system",
                                "content": "actual provider system message",
                            },
                            {"role": "user", "content": "task message"},
                        ],
                    }
                }
            }
        )
        + "\n"
    )

    bundle = attempt / "artifacts/openclaw/bundle"
    _json(
        bundle / "prompts.json",
        {
            "latestSubmittedPrompt": f"prompt for {task}",
            "skillsPrompt": "",
            "system": "assembled system",
        },
    )
    (bundle / "system-prompt.txt").write_text("assembled system\n")
    _json(bundle / "tools.json", {"tools": []})
    bootstrap = attempt / "artifacts/openclaw/bootstrap"
    bootstrap.mkdir(parents=True)
    (bootstrap / "AGENTS.md").write_text("official meta\n")
    (bootstrap / "instruction.md").write_text(f"prompt for {task}\n")

    for phase, entries in {
        "initial": [{"method": "GET", "path": "/health"}],
        "terminal": [
            {"method": "GET", "path": "/health"},
            {"method": "POST", "path": "/agent-write"},
        ],
        "post_verifier": [
            {"method": "GET", "path": "/health"},
            {"method": "POST", "path": "/agent-write"},
            {"method": "GET", "path": "/verifier-read"},
        ],
    }.items():
        phase_root = attempt / "artifacts/env0" / phase
        _json(
            phase_root / "capture-manifest.json",
            {"ok": True, "services": {"mock-gdoc": 9004}},
        )
        service = phase_root / "mock-gdoc"
        _json(service / "state.json", {"phase": phase})
        _json(service / "diff.json", {"phase": phase})
        _json(
            service / "action_log.json",
            {"entries": entries, "count": len(entries)},
        )

    _json(attempt / "verifier/reward.json", {"reward": 0.5})
    return attempt


def _make_fixture(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / "corpus"
    original = _make_attempt(
        run_root / "standard60_v1", "example-task", "original"
    )
    repair = _make_attempt(
        run_root / "standard60_repair_v1", "example-task", "repair"
    )
    _json(run_root / "run-lock.json", {"schema_version": 1})
    _json(
        run_root / "sealed-corpus.json",
        {
            "schema_version": 1,
            "benchmark": "ClawsBench Standard60",
            "model": "openrouter/z-ai/glm-5.2",
            "task_count": 1,
            "canonical_count": 1,
            "canonical_cost_usd": 0.12,
            "canonical_cost_coverage": "2/2",
            "selection_policy": "fixture",
            "tasks": {
                "example-task": {
                    "task": "example-task",
                    "source": "repair_rerun",
                    "rollout": str(repair),
                    "reward": 0.5,
                    "cost_usd": 0.12,
                    "cost_complete": True,
                    "cost_coverage": "2/2",
                    "phase_drift_annotations": [],
                    "post_terminal_activity": {},
                }
            },
            "excluded_original_attempts": {
                "example-task": {
                    "rollout": str(original),
                    "validation": {"ok": False, "errors": ["incomplete"]},
                }
            },
        },
    )
    return run_root, original


def _make_relocated_fixture(tmp_path: Path) -> Path:
    source_root, _ = _make_fixture(tmp_path / "source")
    copied_root = tmp_path / "checkout/eval/clawsbench/corpus"
    shutil.copytree(source_root, copied_root)
    shutil.rmtree(source_root)
    return copied_root


def test_export_uses_seal_and_keeps_every_attempt_and_file(
    tmp_path: Path,
) -> None:
    run_root, original = _make_fixture(tmp_path)
    output = tmp_path / "out"

    report = viewer_export.build_manifests(run_root, output)

    index = json.loads((output / "index.json").read_text())
    task = json.loads((output / "tasks/example-task.json").read_text())
    run = json.loads((output / "run.json").read_text())
    source_files = [path for path in run_root.rglob("*") if path.is_file()]

    assert report["canonical_count"] == 1
    assert report["attempt_count"] == 2
    assert report["artifact_count"] == len(source_files)
    assert index["tasks"][0]["canonical_attempt_id"].endswith("__repair")
    assert {attempt["id"] for attempt in task["attempts"]} == {
        "example-task__original",
        "example-task__repair",
    }
    assert task["canonical"]["prompt"] == "prompt for example-task"
    assert task["canonical"]["source"] == "repair_rerun"
    assert task["canonical"]["services"] == ["mock-gdoc"]
    assert task["canonical"]["instructions"]["actual_system_message"] == (
        "actual provider system message"
    )
    assert task["canonical"]["instructions"]["system_prompt_excerpt"] == (
        "assembled system\n"
    )
    assert task["canonical"]["agent_timeline"][1] == {
        "_malformed": True,
        "line": 2,
        "raw": "not-json",
        "error": "invalid JSON",
    }
    assert task["canonical"]["provider_calls"] == [
        {
            "line": 1,
            "request_timestamp": None,
            "response_timestamp": None,
            "model": "glm",
            "status_code": None,
            "duration_ms": None,
            "finish_reason": None,
            "usage": None,
        }
    ]
    assert task["canonical"]["service_activity"]["mock-gdoc"][
        "agent_entries"
    ] == [{"method": "POST", "path": "/agent-write"}]
    assert task["canonical"]["service_activity"]["mock-gdoc"][
        "verifier_entries"
    ] == [{"method": "GET", "path": "/verifier-read"}]
    assert any(
        artifact["path"] == "trajectory/llm_trajectory.jsonl"
        for artifact in task["canonical"]["artifacts"]
    )
    timeline_artifact = next(
        artifact
        for artifact in task["canonical"]["artifacts"]
        if artifact["path"] == "trajectory/acp_trajectory.jsonl"
    )
    assert timeline_artifact["sha256"] == hashlib.sha256(
        (
            run_root
            / task["attempts"][1]["root"]
            / "trajectory/acp_trajectory.jsonl"
        ).read_bytes()
    ).hexdigest()
    assert any(
        artifact["path"] == "run-lock.json"
        for artifact in run["artifacts"]
    )
    assert any(
        artifact["path"] == "prompts.json"
        for artifact in next(
            item
            for item in task["attempts"]
            if item["root"] == str(original.relative_to(run_root))
        )["artifacts"]
    )


def test_phase_delta_fails_closed_when_logs_are_not_extensions() -> None:
    report = viewer_export.phase_activity(
        {"entries": [{"path": "/initial"}]},
        {"entries": [{"path": "/different"}]},
        {"entries": [{"path": "/different"}, {"path": "/verifier"}]},
    )

    assert report["prefix_ok"] is False
    assert report["agent_entries"] == []
    assert report["verifier_entries"] == []
    assert "not an extension" in report["warning"]


def test_export_rejects_exact_configured_secret(tmp_path: Path) -> None:
    run_root, _ = _make_fixture(tmp_path)
    secret = "sk-or-v1-fixture-secret-value"
    (
        run_root
        / "standard60_repair_v1/example-task__repair/agent/install-stdout.txt"
    ).parent.mkdir(parents=True)
    (
        run_root
        / "standard60_repair_v1/example-task__repair/agent/install-stdout.txt"
    ).write_text(f"leaked={secret}\n")

    with pytest.raises(RuntimeError, match="configured secret"):
        viewer_export.build_manifests(
            run_root,
            tmp_path / "out",
            secret_values=[secret],
        )


def test_configured_secrets_cover_process_and_multiple_env_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_secret = "process-secret-value-123456"
    file_secret = "file-password-value-123456"
    monkeypatch.setenv("VIEWER_TEST_TOKEN", process_secret)
    first = tmp_path / "first.env"
    first.write_text(f"SERVICE_PASSWORD={file_secret}\n")
    second = tmp_path / "second.env"
    second.write_text("PUBLIC_URL=https://example.test\n")

    assert set(viewer_export.configured_secrets(first, second)) >= {
        process_secret,
        file_secret,
    }


def test_export_reanchors_a_sealed_corpus_copy_without_rewriting_the_seal(
    tmp_path: Path,
) -> None:
    run_root = _make_relocated_fixture(tmp_path)
    seal_path = run_root / "sealed-corpus.json"
    sealed_bytes = seal_path.read_bytes()
    output = tmp_path / "out"

    report = viewer_export.build_manifests(run_root, output)

    assert report["canonical_count"] == 1
    assert json.loads((output / "index.json").read_text())["tasks"][0][
        "canonical_attempt_id"
    ] == "example-task__repair"
    assert seal_path.read_bytes() == sealed_bytes


def test_export_reanchors_an_external_suffix_without_resolving_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    recorded = (
        tmp_path
        / "retired-source/standard60_repair_v1/example-task__repair"
    )
    seal_path = run_root / "sealed-corpus.json"
    seal = json.loads(seal_path.read_text())
    seal["tasks"]["example-task"]["rollout"] = str(recorded)
    _json(seal_path, seal)
    original_resolve = Path.resolve

    def reject_recorded_resolve(path: Path, *args, **kwargs) -> Path:
        if path == recorded:
            raise AssertionError("recorded external rollout was resolved")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_recorded_resolve)

    report = viewer_export.build_manifests(run_root, tmp_path / "out")

    assert report["canonical_count"] == 1


def test_export_rejects_an_opaque_recorded_symlink_alias(
    tmp_path: Path,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    canonical = run_root / "standard60_repair_v1/example-task__repair"
    recorded = tmp_path / "retired-source/opaque-alias"
    recorded.parent.mkdir()
    recorded.symlink_to(canonical, target_is_directory=True)
    seal_path = run_root / "sealed-corpus.json"
    seal = json.loads(seal_path.read_text())
    seal["tasks"]["example-task"]["rollout"] = str(recorded)
    _json(seal_path, seal)

    with pytest.raises(RuntimeError, match="cannot prove canonical rollout"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_viewer_prebuild_exports_a_relocated_sealed_corpus(
    tmp_path: Path,
) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is unavailable")
    run_root = _make_relocated_fixture(tmp_path)
    output = tmp_path / "viewer-data"
    completed = subprocess.run(
        [npm, "run", "prebuild"],
        cwd=VIEWER_APP,
        env={
            **os.environ,
            "CLAWSBENCH_CORPUS_ROOT": str(run_root),
            "CLAWSBENCH_VIEWER_DATA": str(output),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((output / "index.json").read_text())["canonical_count"] == 1


@pytest.mark.skipif(
    not (CLAWS_DIR / "corpus/sealed-corpus.json").is_file(),
    reason="default copied ClawsBench corpus is unavailable",
)
def test_viewer_prebuild_exports_the_default_copied_corpus(
    tmp_path: Path,
) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is unavailable")
    output = tmp_path / "viewer-data"
    environment = dict(os.environ)
    environment.pop("CLAWSBENCH_CORPUS_ROOT", None)
    environment["CLAWSBENCH_VIEWER_DATA"] = str(output)
    completed = subprocess.run(
        [npm, "run", "prebuild"],
        cwd=VIEWER_APP,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((output / "index.json").read_text())["canonical_count"] == 60


def test_export_rejects_unproven_external_canonical_rollout(
    tmp_path: Path,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    seal_path = run_root / "sealed-corpus.json"
    seal = json.loads(seal_path.read_text())
    seal["tasks"]["example-task"]["rollout"] = str(
        tmp_path / "outside/example-task__repair"
    )
    _json(seal_path, seal)

    with pytest.raises(RuntimeError, match="cannot prove canonical rollout"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_export_rejects_ambiguous_corpus_relative_rollout_suffix(
    tmp_path: Path,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    seal_path = run_root / "sealed-corpus.json"
    seal = json.loads(seal_path.read_text())
    seal["tasks"]["example-task"]["rollout"] = str(
        tmp_path
        / "standard60_v1/archive/standard60_repair_v1/example-task__repair"
    )
    _json(seal_path, seal)

    with pytest.raises(RuntimeError, match="ambiguous canonical rollout"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_export_rejects_canonical_rollout_phase_mismatch(
    tmp_path: Path,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    seal_path = run_root / "sealed-corpus.json"
    seal = json.loads(seal_path.read_text())
    seal["tasks"]["example-task"]["rollout"] = str(
        tmp_path / "old-corpus/standard60_v1/example-task__repair"
    )
    _json(seal_path, seal)

    with pytest.raises(RuntimeError, match="canonical rollout phase mismatch"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_export_rejects_canonical_seal_task_identity_mismatch(
    tmp_path: Path,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    seal_path = run_root / "sealed-corpus.json"
    seal = json.loads(seal_path.read_text())
    seal["tasks"]["example-task"]["task"] = "other-task"
    _json(seal_path, seal)

    with pytest.raises(RuntimeError, match="canonical seal task mismatch"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_export_rejects_canonical_attempt_identity_mismatch(
    tmp_path: Path,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    other = _make_attempt(
        run_root / "standard60_repair_v1", "other-task", "repair"
    )
    seal_path = run_root / "sealed-corpus.json"
    seal = json.loads(seal_path.read_text())
    seal["tasks"]["example-task"]["rollout"] = str(other)
    _json(seal_path, seal)

    with pytest.raises(RuntimeError, match="canonical attempt identity mismatch"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_export_rejects_relocated_attempt_symlink_alias(
    tmp_path: Path,
) -> None:
    run_root = _make_relocated_fixture(tmp_path)
    original = run_root / "standard60_v1/example-task__original"
    repair = run_root / "standard60_repair_v1/example-task__repair"
    shutil.rmtree(repair)
    repair.symlink_to(original, target_is_directory=True)

    with pytest.raises(RuntimeError, match="canonical rollout symlink"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_export_rejects_canonical_result_task_identity_mismatch(
    tmp_path: Path,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    result_path = run_root / (
        "standard60_repair_v1/example-task__repair/result.json"
    )
    result = json.loads(result_path.read_text())
    result["task_name"] = "other-task"
    _json(result_path, result)

    with pytest.raises(RuntimeError, match="canonical attempt identity mismatch"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_export_rejects_a_symlinked_canonical_result(
    tmp_path: Path,
) -> None:
    run_root, _ = _make_fixture(tmp_path)
    attempt = run_root / "standard60_repair_v1/example-task__repair"
    result_path = attempt / "result.json"
    result = json.loads(result_path.read_text())
    result["rollout_name"] = attempt.name
    external_result = tmp_path / "external-result.json"
    _json(external_result, result)
    result_path.unlink()
    result_path.symlink_to(external_result)

    with pytest.raises(RuntimeError, match="canonical attempt identity mismatch"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_export_rejects_same_task_different_attempt_result_identity(
    tmp_path: Path,
) -> None:
    run_root, original = _make_fixture(tmp_path)
    result_path = run_root / (
        "standard60_repair_v1/example-task__repair/result.json"
    )
    result = json.loads(result_path.read_text())
    result["rollout_name"] = original.name
    _json(result_path, result)

    with pytest.raises(RuntimeError, match="canonical attempt identity mismatch"):
        viewer_export.build_manifests(run_root, tmp_path / "out")


def test_claws_export_entrypoint_exports_without_superseded_normalizer(
    tmp_path: Path,
) -> None:
    for relative in (
        "eval/coverage/agentdojo/analysis/trace_adapter.py",
        "eval/coverage/tau3/analysis/trace_adapter.py",
    ):
        assert not (PROTOTYPE_ROOT / relative).exists()
    run_root, _ = _make_fixture(tmp_path)
    output = tmp_path / "normalized"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "eval.clawsbench.export",
            "--claws-run-root",
            str(run_root),
            "--claws-output",
            str(output),
        ],
        cwd=PROTOTYPE_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((output / "index.json").read_text())[
        "canonical_count"
    ] == 1
    assert "exporting ClawsBench" in completed.stdout


    assert not (PROTOTYPE_ROOT / "eval/normalize.py").exists()


@pytest.mark.skipif(
    not (REAL_RUN_ROOT / "sealed-corpus.json").is_file(),
    reason="local sealed ClawsBench corpus is unavailable",
)
def test_real_corpus_audit_is_complete(tmp_path: Path) -> None:
    report = viewer_export.build_manifests(
        REAL_RUN_ROOT,
        tmp_path / "real",
        secret_values=viewer_export.configured_secrets(
            PROTOTYPE_ROOT / "eval/.env"
        ),
    )

    assert report["canonical_count"] == 60
    assert report["attempt_count"] == 61
    assert report["artifact_count"] == 4558


def test_published_sample_is_self_contained(tmp_path: Path) -> None:
    run_root, _ = _make_fixture(tmp_path)
    output = tmp_path / "published"

    report = viewer_export.build_published_sample(
        run_root,
        output,
        tasks=["example-task"],
    )

    index = json.loads((output / "data/index.json").read_text())
    detail = json.loads(
        (output / "data/tasks/example-task.json").read_text()
    )
    run = json.loads((output / "data/run.json").read_text())
    seal = json.loads((output / "corpus/sealed-corpus.json").read_text())

    assert report == {
        "canonical_count": 1,
        "attempt_count": 2,
        "artifact_count": 0,
        "output": str(output),
    }
    assert index["sample"] is True
    assert index["full_canonical_count"] == 1
    assert index["canonical_count"] == 1
    assert [item["task"] for item in index["tasks"]] == ["example-task"]
    assert index["tasks"][0]["canonical_attempt_id"].endswith("__repair")
    assert run["sample"] is True
    assert run["artifacts"] == []
    assert seal["sample"] is True
    assert seal["canonical_count"] == 1
    assert set(seal["tasks"]) == {"example-task"}
    assert detail["canonical"]["artifacts"] == []
    for attempt in detail["attempts"]:
        assert attempt["root"] == f"published-sample/{attempt['id']}"
        assert attempt["artifacts"] == []
    for path in (output / "data").rglob("*.json"):
        assert "/Users/" not in path.read_text()


def test_published_sample_task_set_is_fixed() -> None:
    assert viewer_export.PUBLISHED_SAMPLE_TASKS == (
        "auth-app-install-scope-eval",
        "email-workflow-cleanup-and-report",
        "multi-doc-slack-spec-drift",
        "slack-channel-reorg",
        "stripe-refund-correct-customer",
    )
