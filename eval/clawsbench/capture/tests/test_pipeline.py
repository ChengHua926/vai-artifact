from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


CAPTURE_DIR = Path(__file__).resolve().parents[1]
CLAWS_DIR = CAPTURE_DIR.parent
sys.path.insert(0, str(CAPTURE_DIR))


def _load_capture_run():
    spec = importlib.util.spec_from_file_location(
        "clawsbench_capture_run_test", CAPTURE_DIR / "run.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run = _load_capture_run()
RESEARCH_ROOT = run.RESEARCH_ROOT


def test_standard60_and_smoke_selection_are_exact() -> None:
    standard = run.load_standard60(run.ENV0_ROOT)
    assert len(standard) == 60
    assert len(set(standard)) == 60
    assert standard == sorted(standard)
    assert run.select_tasks("standard60", run.ENV0_ROOT) == standard
    assert run.select_tasks("smoke", run.ENV0_ROOT) == [
        "gdoc-edit-append-status",
        "multi-misread-approval-scope",
    ]


def test_meta_is_exact_official_file_and_not_a_skill() -> None:
    assert hashlib.sha256(run.META_PATH.read_bytes()).hexdigest() == (
        "279141b795d557ecdad7c69ae29a4ede0ad827b1fb909288c6485d78928e286a"
    )
    assert not (CLAWS_DIR / "skills").exists()


def test_active_services_come_from_task_metadata() -> None:
    assert run.active_services(
        run.ENV0_ROOT / "tasks/gdoc-edit-append-status"
    ) == {
        "mock-gdrive": 9003,
        "mock-gdoc": 9004,
    }
    assert run.active_services(
        run.ENV0_ROOT / "tasks/multi-misread-approval-scope"
    ) == {
        "mock-gmail": 9001,
        "mock-gdrive": 9003,
        "mock-gdoc": 9004,
        "mock-slack": 9005,
    }


def test_clean_registry_command_is_pinned_without_mutating_source() -> None:
    clean = (
        "BF_NODE_VERSION=22.20.0; "
        "npm install -g --prefix /opt/benchflow/js-agents openclaw@latest"
    )
    pinned = run.pin_openclaw_install_command(clean)
    assert "BF_NODE_VERSION=24.15.0" in pinned
    assert "openclaw@2026.7.1" in pinned
    assert "22.20.0" not in pinned
    assert "openclaw@latest" not in pinned
    with pytest.raises(RuntimeError):
        run.pin_openclaw_install_command(pinned)


def test_run_lock_refuses_configuration_drift(tmp_path: Path) -> None:
    lock = tmp_path / "run-lock.json"
    spec = {"schema_version": 1, "model": run.MODEL, "tasks": ["a", "b"]}
    assert run.ensure_run_lock(lock, spec) == "created"
    assert run.ensure_run_lock(lock, spec) == "matched"
    with pytest.raises(RuntimeError, match="configuration drift"):
        run.ensure_run_lock(lock, {**spec, "model": "different"})


def _write_capture_fixture(rollout: Path, task: str) -> None:
    (rollout / "trajectory").mkdir(parents=True)
    (rollout / "artifacts/openclaw/raw").mkdir(parents=True)
    (rollout / "artifacts/openclaw/bundle").mkdir(parents=True)
    (rollout / "artifacts/env0").mkdir(parents=True)
    (rollout / "verifier").mkdir()
    (rollout / "result.json").write_text(
        json.dumps(
            {
                "task_name": task,
                "model": run.MODEL,
                "skill_mode": "no-skill",
                "n_skill_invocations": 0,
                "rewards": {"reward": 0.0},
                "verifier_error": None,
            }
        )
    )
    (rollout / "prompts.json").write_text(json.dumps(["original task prompt"]))
    (rollout / "trajectory/acp_trajectory.jsonl").write_text('{"type":"tool_call"}\n')
    (rollout / "trajectory/llm_trajectory.jsonl").write_text(
        json.dumps(
            {
                "request": {
                    "body": {
                        "model": run.OPENROUTER_MODEL,
                        "messages": [
                            {
                                "role": "system",
                                "content": run.META_PATH.read_text(),
                            },
                            {"role": "user", "content": "original task prompt"},
                        ],
                    }
                },
                "response": {"status_code": 200},
            }
        )
        + "\n"
    )
    (rollout / "artifacts/openrouter-generations.json").parent.mkdir(
        parents=True, exist_ok=True
    )
    (rollout / "artifacts/openrouter-generations.json").write_text(
        json.dumps(
            {
                "ok": True,
                "request_count": 1,
                "generation_count": 1,
                "total_cost_usd": 0.01,
                "cost_complete": True,
                "cost_coverage": "1/1",
            }
        )
    )
    (rollout / "artifacts/openclaw/raw/session.jsonl").write_text(
        json.dumps({"toolResult": "x" * 1500}) + "\n"
    )
    for name in run.REQUIRED_BUNDLE_FILES:
        payload = run.META_PATH.read_text() if name == "system-prompt.txt" else "{}\n"
        (rollout / "artifacts/openclaw/bundle" / name).write_text(payload)
    services = {"mock-gdoc": 9004}
    for phase in ("initial", "terminal", "post_verifier"):
        service_dir = rollout / "artifacts/env0" / phase / "mock-gdoc"
        service_dir.mkdir(parents=True)
        for endpoint in ("state", "diff", "action_log"):
            (service_dir / f"{endpoint}.json").write_text("{}\n")
            (service_dir / f"{endpoint}.meta.json").write_text(
                json.dumps(
                    {
                        "http_status": 200,
                        "bytes": 3,
                        "sha256": hashlib.sha256(b"{}\n").hexdigest(),
                        "error": None,
                    }
                )
            )
        (rollout / "artifacts/env0" / phase / "capture-manifest.json").write_text(
            json.dumps({"ok": True, "services": services})
        )
    (rollout / "artifacts/capture-status.json").write_text(
        json.dumps(
            {
                "bootstrap": {"ok": True},
                "initial": {"ok": True},
                "terminal": {"ok": True},
                "openclaw": {"ok": True},
                "post_verifier": {"ok": True},
            }
        )
    )
    (rollout / "verifier/reward.json").write_text('{"reward": 0.0}\n')


def test_capture_validator_requires_complete_native_and_env0_evidence(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "task__abc"
    _write_capture_fixture(rollout, "task")
    report = run.validate_rollout_capture(
        rollout,
        expected_task="task",
        services={"mock-gdoc": 9004},
        expected_prompt="original task prompt",
    )
    assert report["ok"] is True
    assert (
        rollout / "artifacts/openclaw/raw/session.jsonl"
    ).stat().st_size > 1000

    (rollout / "artifacts/env0/terminal/mock-gdoc/action_log.json").unlink()
    report = run.validate_rollout_capture(
        rollout,
        expected_task="task",
        services={"mock-gdoc": 9004},
        expected_prompt="original task prompt",
    )
    assert report["ok"] is False
    assert any("action_log.json" in error for error in report["errors"])


def test_capture_validator_rejects_verifier_environment_mutation(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "task__abc"
    _write_capture_fixture(rollout, "task")
    (
        rollout
        / "artifacts/env0/post_verifier/mock-gdoc/state.json"
    ).write_text('{"mutated": true}\n')
    report = run.validate_rollout_capture(
        rollout,
        expected_task="task",
        services={"mock-gdoc": 9004},
        expected_prompt="original task prompt",
    )
    assert report["ok"] is False
    assert any("verifier mutated mock-gdoc state" in error for error in report["errors"])


def test_capture_validator_ignores_snapshot_timestamp_only(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "task__abc"
    _write_capture_fixture(rollout, "task")
    terminal = rollout / "artifacts/env0/terminal/mock-gdoc/state.json"
    post = rollout / "artifacts/env0/post_verifier/mock-gdoc/state.json"
    terminal.write_text('{"timestamp": "before", "items": [1]}\n')
    post.write_text('{"timestamp": "after", "items": [1]}\n')
    report = run.validate_rollout_capture(
        rollout,
        expected_task="task",
        services={"mock-gdoc": 9004},
        expected_prompt="original task prompt",
    )
    assert report["ok"] is True


def test_openrouter_generation_reconciliation_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollout = tmp_path / "task__abc"
    trajectory = rollout / "trajectory/llm_trajectory.jsonl"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(
        "\n".join(
            json.dumps({"response": {"body": {"id": response_id}}})
            for response_id in ("gen-a", "gen-b", "gen-a")
        )
        + "\n"
    )
    payloads = {
        "gen-a": {
            "data": {
                "id": "gen-a",
                "model": "z-ai/glm-5.2-20260616",
                "provider_name": "Provider A",
                "total_cost": 0.0123,
            }
        },
        "gen-b": {
            "data": {
                "id": "gen-b",
                "model": "z-ai/glm-5.2-20260616",
                "provider_name": "Provider B",
                "total_cost": 0.0045,
            }
        },
    }

    def fake_request(url: str, key: str, *, payload=None):  # noqa: ANN001
        assert key == "secret"
        assert payload is None
        return payloads[url.rsplit("=", 1)[1]]

    monkeypatch.setattr(run, "_openrouter_request", fake_request)
    report = run.reconcile_openrouter_generations(rollout, "secret")
    assert report["ok"] is True
    assert report["request_count"] == 2
    assert report["generation_count"] == 2
    assert report["total_cost_usd"] == 0.0168
    assert report["cost_complete"] is True
    assert report["providers"] == ["Provider A", "Provider B"]


def test_openrouter_generation_reconciliation_records_unpriced_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollout = tmp_path / "task__abc"
    trajectory = rollout / "trajectory/llm_trajectory.jsonl"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(
        "\n".join(
            (
                json.dumps({"response": {"body": {"id": "gen-priced"}}}),
                json.dumps({"response": {"body": {"id": "gen-missing"}}}),
            )
        )
        + "\n"
    )

    def fake_request(url: str, key: str, *, payload=None):  # noqa: ANN001
        if url.endswith("gen-missing"):
            raise RuntimeError("HTTP 404: generation not found")
        return {
            "data": {
                "id": "gen-priced",
                "model": "z-ai/glm-5.2-20260616",
                "provider_name": "Provider A",
                "total_cost": 0.0123,
            }
        }

    monkeypatch.setattr(run, "_openrouter_request", fake_request)
    report = run.reconcile_openrouter_generations(rollout, "secret")
    assert report["ok"] is True
    assert report["cost_complete"] is False
    assert report["cost_coverage"] == "1/2"
    assert report["total_cost_usd"] == 0.0123
    assert report["unpriced_request_ids"] == ["gen-missing"]
    assert len(report["errors"]) == 1


def test_capture_script_uses_stable_container_path() -> None:
    assert run.CONTAINER_CAPTURE_SCRIPT.startswith("/opt/benchflow/")
    assert not run.CONTAINER_CAPTURE_SCRIPT.startswith("/tmp/")


def test_standard60_cost_estimate_scales_unpriced_smoke_requests() -> None:
    estimate = run.estimate_standard60_cost(
        {
            "valid_count": 2,
            "total_cost_usd": 0.07,
            "tasks": {
                "a": {"cost_coverage": "11/12"},
                "b": {"cost_coverage": "20/20"},
            },
        }
    )
    assert estimate["priced_generations"] == 31
    assert estimate["requested_generations"] == 32
    assert estimate["observed_cost_lower_bound_usd"] == 0.07
    assert estimate["estimated_standard60_cost_usd"] == 2.1677


def test_dry_run_shell_plans_60_without_network_or_docker() -> None:
    script = CAPTURE_DIR / "run_glm52_standard60.sh"
    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        cwd=RESEARCH_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["mode"] == "standard60"
    assert plan["task_count"] == 60
    assert plan["model"] == run.MODEL
    assert plan["skills"] == "off"
    assert plan["meta"] == "system"
    assert plan["would_start_docker"] is False
    assert plan["would_call_openrouter"] is False
