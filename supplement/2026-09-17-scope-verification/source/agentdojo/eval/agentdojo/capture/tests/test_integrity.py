from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


os.environ.setdefault("OPENROUTER_API_KEY", "offline-test-key")

from eval.agentdojo.capture import integrity, run  # noqa: E402


def _trace(cell: dict[str, str], *, error: str | None = None, messages: list[dict] | None = None) -> dict:
    return {
        "suite_name": cell["suite_name"],
        "user_task_id": cell["user_task_id"],
        "attack_type": None if cell["attack_type"] == "none" else cell["attack_type"],
        "injection_task_id": None if cell["injection_task_id"] == "none" else cell["injection_task_id"],
        "utility": True,
        "security": False,
        "error": error,
        "messages": messages
        if messages is not None
        else [
            {"role": "user", "content": [{"type": "text", "content": "do work"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"function": "lookup"}]},
            {"role": "tool", "content": [{"type": "text", "content": "result"}], "error": None},
        ],
    }


def _write_trace(logdir: Path, name: str, trace: dict) -> None:
    path = logdir / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace))


def test_provider_preferences_pin_the_exact_tag_without_fallbacks() -> None:
    assert integrity.provider_preferences("deepinfra/bf16") == {
        "provider": {
            "order": ["deepinfra/bf16"],
            "allow_fallbacks": False,
        }
    }


def test_raw_response_summary_rejects_mixed_providers(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    raw.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "requested_model": "z-ai/glm-4.7-flash",
                    "response_model": "z-ai/glm-4.7-flash",
                    "provider": "deepinfra/bf16",
                    "finish_reason": "stop",
                    "usage": {"total_tokens": 10},
                    "billed_cost_usd": 0.01,
                },
                {
                    "requested_model": "z-ai/glm-4.7-flash",
                    "response_model": "z-ai/glm-4.7-flash",
                    "provider": "other/provider",
                    "finish_reason": "stop",
                    "usage": {"total_tokens": 11},
                    "billed_cost_usd": 0.02,
                },
            ]
        )
        + "\n"
    )

    summary = integrity.summarize_raw_responses(raw)

    assert summary["ok"] is False
    assert summary["providers"] == ["deepinfra/bf16", "other/provider"]
    assert summary["errors"] == ["mixed providers in raw responses"]


def test_raw_response_summary_sums_billed_costs(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    raw.write_text(
        json.dumps(
            {
                "requested_model": "z-ai/glm-4.7-flash",
                "response_model": "z-ai/glm-4.7-flash",
                "provider": "deepinfra/bf16",
                "finish_reason": "stop",
                "usage": {"total_tokens": 10},
                "billed_cost_usd": 0.0125,
            }
        )
        + "\n"
        + json.dumps(
            {
                "requested_model": "z-ai/glm-4.7-flash",
                "response_model": "z-ai/glm-4.7-flash",
                "provider": "deepinfra/bf16",
                "finish_reason": "tool_calls",
                "usage": {"total_tokens": 20},
                "billed_cost_usd": 0.0075,
            }
        )
        + "\n"
    )

    summary = integrity.summarize_raw_responses(raw)

    assert summary["ok"] is True
    assert summary["response_count"] == 2
    assert summary["total_billed_cost_usd"] == 0.02
    assert summary["total_tokens"] == 30


def test_raw_response_validation_separates_response_alias_from_endpoint_revision(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    raw.write_text(
        json.dumps(
            {
                "requested_model": "z-ai/glm-4.7-flash",
                "requested_provider_tag": "deepinfra/bf16",
                "response_model": "z-ai/glm-4.7-flash",
                "provider": "DeepInfra",
                "finish_reason": "stop",
                "usage": {"total_tokens": 10},
                "billed_cost_usd": 0.01,
            }
        )
        + "\n"
    )

    validation = integrity.validate_raw_response_summary(
        integrity.summarize_raw_responses(raw),
        model_id="z-ai/glm-4.7-flash",
        provider="deepinfra/bf16",
        response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.35,
    )

    assert validation["ok"] is True
    assert validation["providers"] == ["DeepInfra"]
    assert validation["requested_provider_tags"] == ["deepinfra/bf16"]
    assert validation["response_models"] == ["z-ai/glm-4.7-flash"]
    assert validation["endpoint_revision"] == "z-ai/glm-4.7-flash-20260119"


def test_raw_response_validation_requires_one_or_more_attempts_for_every_trace_cell(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    cell = {"suite_name": "banking", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}
    raw.write_text(json.dumps({
        "requested_model": "z-ai/glm-4.7-flash", "requested_provider_tag": "deepinfra/bf16",
        "response_model": "z-ai/glm-4.7-flash", "provider": "DeepInfra",
        "usage": {"total_tokens": 10}, "billed_cost_usd": 0.01, "trace_cell": cell,
    }) + "\n")

    validation = integrity.validate_raw_response_summary(
        integrity.summarize_raw_responses(raw), model_id="z-ai/glm-4.7-flash",
        provider="deepinfra/bf16", response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.35, expected_cells=[cell, {**cell, "user_task_id": "user_task_1"}],
    )

    assert validation["ok"] is False
    assert "raw responses do not cover every expected trace cell" in validation["errors"]


def test_raw_response_validation_rejects_orphan_row_even_when_expected_cells_are_covered(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    cell = {"suite_name": "banking", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}
    valid = {
        "attempt_id": 1, "response_id": "response-1", "requested_model": "z-ai/glm-4.7-flash",
        "requested_provider_tag": "deepinfra/bf16", "response_model": "z-ai/glm-4.7-flash",
        "provider": "DeepInfra", "finish_reason": "stop", "usage": {"total_tokens": 10},
        "billed_cost_usd": 0.01, "trace_cell": cell,
    }
    raw.write_text(json.dumps(valid) + "\n" + json.dumps({**valid, "attempt_id": None, "response_id": None, "trace_cell": None}) + "\n")

    validation = integrity.validate_raw_response_summary(
        integrity.summarize_raw_responses(raw), model_id="z-ai/glm-4.7-flash",
        provider="deepinfra/bf16", response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.35, expected_cells=[cell],
    )

    assert validation["ok"] is False
    assert "invalid raw response row 2: attempt_id" in validation["errors"]
    assert "invalid raw response row 2: response_id" in validation["errors"]
    assert "invalid raw response row 2: trace_cell" in validation["errors"]


def test_raw_response_validation_rejects_duplicate_or_nonmonotone_attempts(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    first = {"suite_name": "banking", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}
    second = {**first, "user_task_id": "user_task_1"}
    def row(attempt_id: int, response_id: str, cell: dict[str, str]) -> dict:
        return {
            "attempt_id": attempt_id, "response_id": response_id, "requested_model": "z-ai/glm-4.7-flash",
            "requested_provider_tag": "deepinfra/bf16", "response_model": "z-ai/glm-4.7-flash",
            "provider": "DeepInfra", "finish_reason": "stop", "usage": {"total_tokens": 10},
            "billed_cost_usd": 0.01, "trace_cell": cell,
        }
    raw.write_text(json.dumps(row(2, "a", first)) + "\n" + json.dumps(row(2, "b", second)) + "\n")

    validation = integrity.validate_raw_response_summary(
        integrity.summarize_raw_responses(raw), model_id="z-ai/glm-4.7-flash",
        provider="deepinfra/bf16", response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.35, expected_cells=[first, second],
    )

    assert validation["ok"] is False
    assert "duplicate raw response attempt_id" in validation["errors"]
    assert "raw response attempt_id is not monotone" in validation["errors"]


@pytest.mark.parametrize("cost", ["NaN", "Infinity", -0.01])
def test_raw_response_summary_rejects_nonfinite_or_negative_costs(tmp_path: Path, cost: object) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    raw.write_text(json.dumps({
        "requested_model": "z-ai/glm-4.7-flash", "requested_provider_tag": "deepinfra/bf16",
        "response_model": "z-ai/glm-4.7-flash", "provider": "DeepInfra",
        "usage": {"total_tokens": 10}, "billed_cost_usd": cost,
    }) + "\n")

    summary = integrity.summarize_raw_responses(raw)

    assert summary["cost_complete"] is False
    assert "invalid billed cost" in summary["errors"]


def test_trace_corpus_rejects_duplicate_and_missing_expected_cells(tmp_path: Path) -> None:
    expected = [
        {"suite_name": "banking", "user_task_id": "user_task_0", "attack_type": "important_instructions", "injection_task_id": "injection_task_0"},
        {"suite_name": "banking", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"},
    ]
    _write_trace(tmp_path, "first", _trace(expected[0]))
    _write_trace(tmp_path, "duplicate", _trace(expected[0]))

    report = integrity.validate_trace_corpus(tmp_path, expected)

    assert report["ok"] is False
    assert report["duplicate_cells"] == [expected[0]]
    assert report["missing_cells"] == [expected[1]]


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": [{"type": "text", "content": "refuse"}]}],
    [
        {"role": "user", "content": [{"type": "text", "content": "do work"}]},
        {"role": "assistant", "content": None, "tool_calls": [{"function": "lookup"}]},
        {"role": "tool", "content": [], "error": "ValueError: No events found"},
        {"role": "assistant", "content": [{"type": "text", "content": "recovered"}], "tool_calls": []},
    ],
])
def test_trace_corpus_preserves_valid_no_tool_and_recoverable_tool_error_outcomes(tmp_path: Path, messages: list[dict]) -> None:
    expected = [{"suite_name": "slack", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}]
    _write_trace(tmp_path, "trace", _trace(expected[0], messages=messages))

    assert integrity.validate_trace_corpus(tmp_path, expected)["ok"] is True


def test_trace_corpus_rejects_mismatched_frozen_provenance(tmp_path: Path) -> None:
    expected = [{"suite_name": "slack", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}]
    trace = _trace(expected[0])
    trace["benchmark_version"] = "v1.2.1"
    trace["agentdojo_package_version"] = "0.1.35"
    _write_trace(tmp_path, "trace", trace)

    report = integrity.validate_trace_corpus(
        tmp_path, expected,
        expected_provenance={"benchmark_version": "v1.2.2", "agentdojo_package_version": "0.1.35"},
    )

    assert report["ok"] is False
    assert report["invalid_traces"][0]["errors"] == ["trace provenance mismatch: benchmark_version"]


def test_trace_corpus_reports_multiple_missing_cells_in_stable_order(tmp_path: Path) -> None:
    expected = [
        {"suite_name": "banking", "user_task_id": "user_task_1", "attack_type": "none", "injection_task_id": "none"},
        {"suite_name": "banking", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"},
    ]

    report = integrity.validate_trace_corpus(tmp_path, expected)

    assert report["missing_cells"] == [expected[1], expected[0]]


@pytest.mark.parametrize(
    ("trace", "expected_error"),
    [
        (_trace({"suite_name": "slack", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}, error="provider failed"), "trace error"),
        (_trace({"suite_name": "slack", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}, messages=[]), "empty messages"),
    ],
)
def test_trace_corpus_rejects_errored_or_empty_traces(tmp_path: Path, trace: dict, expected_error: str) -> None:
    expected = [{"suite_name": "slack", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}]
    _write_trace(tmp_path, "trace", trace)

    report = integrity.validate_trace_corpus(tmp_path, expected)

    assert report["ok"] is False
    assert expected_error in report["invalid_traces"][0]["errors"]


def test_actual_agentdojo_smoke_suite_definitions_produce_exactly_70_attack_and_8_benign_cells() -> None:
    cells = run.expected_cells_for_run(run.SUITES, "v1.2.2", True)

    assert len(cells) == 78
    assert sum(cell["attack_type"] == "important_instructions" for cell in cells) == 70
    assert sum(cell["attack_type"] == "none" for cell in cells) == 8


def test_capture_lock_refuses_dirty_or_mismatched_resume_directory(tmp_path: Path) -> None:
    config = {"model_id": "z-ai/glm-4.7-flash", "temperature": 0.0, "provider": "deepinfra/bf16"}
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "raw_responses.jsonl").write_text("")
    with pytest.raises(RuntimeError, match="no capture lock"):
        integrity.ensure_capture_lock(dirty, config)

    clean = tmp_path / "clean"
    assert integrity.ensure_capture_lock(clean, config) == "created"
    assert integrity.ensure_capture_lock(clean, config) == "matched"
    with pytest.raises(RuntimeError, match="configuration drift"):
        integrity.ensure_capture_lock(clean, {**config, "temperature": 0.7})


def test_matched_lock_preflight_rejects_dirty_raw_artifact_before_capture(tmp_path: Path) -> None:
    cell = {"suite_name": "banking", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}
    config = {
        "model_id": "z-ai/glm-4.7-flash", "provider": "deepinfra/bf16",
        "response_model": "z-ai/glm-4.7-flash",
        "endpoint_revision": "z-ai/glm-4.7-flash-20260119",
        "max_cost_usd": 0.35, "expected_trace_cells": [cell],
        "benchmark_version": "v1.2.2", "agentdojo_version": "0.1.35",
    }
    assert integrity.ensure_capture_lock(tmp_path, config) == "created"
    (tmp_path / "raw_responses.jsonl").write_text(json.dumps({
        "attempt_id": 1, "response_id": "response-1", "requested_model": config["model_id"],
        "requested_provider_tag": "wrong/provider", "response_model": config["response_model"],
        "provider": "DeepInfra", "finish_reason": "stop", "usage": {"total_tokens": 10},
        "billed_cost_usd": 0.01, "trace_cell": cell,
    }) + "\n")

    report = integrity.preflight_capture_artifacts(tmp_path, config)

    assert report["ok"] is False
    assert "invalid raw response row 1: requested_provider_tag" in report["raw_responses"]["errors"]


def test_matched_lock_preflight_allows_valid_partial_cells_but_rejects_unexpected_or_malformed_rows(tmp_path: Path) -> None:
    first = {"suite_name": "banking", "user_task_id": "user_task_0", "attack_type": "none", "injection_task_id": "none"}
    second = {**first, "user_task_id": "user_task_1"}
    config = {
        "model_id": "z-ai/glm-4.7-flash", "provider": "deepinfra/bf16",
        "response_model": "z-ai/glm-4.7-flash",
        "endpoint_revision": "z-ai/glm-4.7-flash-20260119", "max_cost_usd": 0.35,
        "expected_trace_cells": [first, second], "benchmark_version": "v1.2.2", "agentdojo_version": "0.1.35",
    }
    assert integrity.ensure_capture_lock(tmp_path, config) == "created"
    (tmp_path / "raw_responses.jsonl").write_text(json.dumps({
        "attempt_id": 1, "response_id": "response-1", "requested_model": config["model_id"],
        "requested_provider_tag": config["provider"], "response_model": config["response_model"],
        "provider": "DeepInfra", "finish_reason": "stop", "usage": {"total_tokens": 10},
        "billed_cost_usd": 0.01, "trace_cell": first,
    }) + "\n")
    trace = _trace(first)
    trace.update({"benchmark_version": "v1.2.2", "agentdojo_package_version": "0.1.35"})
    _write_trace(tmp_path / "agentdojo", "first", trace)

    report = integrity.preflight_capture_artifacts(tmp_path, config)

    assert report["ok"] is True
    assert report["state"] == "partial"
    assert report["trace_corpus"]["missing_cells"] == [second]

    with (tmp_path / "raw_responses.jsonl").open("a") as raw:
        raw.write(json.dumps({
        "attempt_id": 2, "response_id": "response-2", "requested_model": config["model_id"],
        "requested_provider_tag": config["provider"], "response_model": config["response_model"],
        "provider": "DeepInfra", "finish_reason": "stop", "usage": {"total_tokens": 10},
        "billed_cost_usd": 0.01, "trace_cell": {**first, "user_task_id": "unexpected"},
        }) + "\n")

    report = integrity.preflight_capture_artifacts(tmp_path, config)

    assert report["ok"] is False
    assert "invalid raw response row 2: trace_cell" in report["raw_responses"]["errors"]

    with (tmp_path / "raw_responses.jsonl").open("a") as raw:
        raw.write(json.dumps({
            "attempt_id": 3, "response_id": None, "requested_model": config["model_id"],
            "requested_provider_tag": config["provider"], "response_model": config["response_model"],
            "provider": "DeepInfra", "finish_reason": "stop", "usage": {"total_tokens": 10},
            "billed_cost_usd": 0.01, "trace_cell": first,
        }) + "\n")

    report = integrity.preflight_capture_artifacts(tmp_path, config)

    assert report["ok"] is False
    assert "invalid raw response row 3: response_id" in report["raw_responses"]["errors"]


class _Usage:
    def __init__(self, cost: object) -> None:
        self.cost = cost

    def model_dump(self) -> dict:
        return {"total_tokens": 10, "cost": self.cost}


class _Response:
    def __init__(self, cost: object, choices: list[object], response_id: str) -> None:
        self.usage = _Usage(cost)
        self.choices = choices
        self.id = response_id
        self.model = "z-ai/glm-4.7-flash"
        self.provider = "DeepInfra"

    def model_dump(self) -> dict[str, object]:
        return {
            "id": self.id,
            "model": self.model,
            "provider": self.provider,
            "choices": [],
            "diagnostic": {"error": "captured verbatim"},
        }


class _Choice:
    finish_reason = "stop"


class _Client:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


CELL = {
    "suite_name": "banking", "user_task_id": "user_task_0",
    "attack_type": "none", "injection_task_id": "none",
}


class _ReturnedErrorResponse:
    class _ZeroUsage:
        def model_dump(self) -> dict[str, int]:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    usage = _ZeroUsage()
    choices = None
    id = None
    model = "z-ai/glm-4.7-flash"
    provider = "DeepInfra"

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        return {
            "id": None,
            "model": self.model,
            "provider": self.provider,
            "choices": None,
            "usage": self.usage.model_dump(),
            "error": {
                "code": 429,
                "message": "Provider returned an error",
                "metadata": {"provider_name": "DeepInfra"},
            },
            "messages": [{"role": "user", "content": "private prompt"}],
            "headers": {"authorization": "Bearer private-key"},
            "debug": {"data": "raw diagnostic"},
        }


class _ReturnedOutputErrorResponse(_ReturnedErrorResponse):
    choices = [_Choice()]

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        payload = super().model_dump(mode=mode)
        payload["choices"] = [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "generated output"},
        }]
        return payload


class _ConflictingRouteErrorResponse(_ReturnedErrorResponse):
    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        payload = super().model_dump(mode=mode)
        payload["error"]["metadata"].update({
            "provider_name": "WrongProvider",
            "model": "wrong/model",
        })
        return payload


class _ConflictingModelAliasErrorResponse(_ReturnedErrorResponse):
    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        payload = super().model_dump(mode=mode)
        payload["error"]["metadata"].update({
            "model": "z-ai/glm-4.7-flash",
            "model_id": "wrong/model",
        })
        return payload


def _sdk_rate_limit(body: dict[str, object]):
    import httpx
    import openai

    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        json=body,
    )
    return openai.RateLimitError("rate limit", response=response, body=body)


@pytest.mark.parametrize("source", ["returned", "sdk"])
def test_conflicting_top_level_and_error_route_identity_latches_capture_closed(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    if source == "returned":
        conflicting = _ConflictingRouteErrorResponse()
    else:
        conflicting = _sdk_rate_limit({
            "model": "z-ai/glm-4.7-flash",
            "provider": "DeepInfra",
            "error": {
                "code": 429,
                "metadata": {"provider_name": "WrongProvider", "model": "wrong/model"},
            },
            "choices": [],
            "usage": {"completion_tokens": 0},
        })
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    client = _Client([conflicting, _Response("0.01", [_Choice()], "must-not-run")])

    with pytest.raises(run.BudgetExceeded, match="conflicting response route identity"):
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)

    assert client.calls == 1
    record = json.loads(raw.read_text())
    assert record["response_metadata"]["route_conflicts"] == {
        "model": ["z-ai/glm-4.7-flash", "wrong/model"],
        "provider": ["DeepInfra", "WrongProvider"],
    }
    validation = integrity.validate_raw_response_summary(
        integrity.summarize_raw_responses(raw),
        model_id="z-ai/glm-4.7-flash", provider="deepinfra/bf16",
        response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.10, expected_cells=[],
    )
    assert validation["ok"] is False
    assert "invalid raw response row 1: route identity conflict" in validation["errors"]
    with pytest.raises(run.BudgetExceeded, match="conflicting response route identity"):
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)
    assert client.calls == 1


@pytest.mark.parametrize("source", ["returned", "sdk"])
def test_conflicting_nested_model_aliases_latch_capture_closed(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    if source == "returned":
        conflicting = _ConflictingModelAliasErrorResponse()
    else:
        conflicting = _sdk_rate_limit({
            "model": "z-ai/glm-4.7-flash",
            "provider": "DeepInfra",
            "error": {
                "code": 429,
                "metadata": {
                    "provider_name": "DeepInfra",
                    "model": "z-ai/glm-4.7-flash",
                    "model_id": "wrong/model",
                },
            },
            "choices": [],
            "usage": {"completion_tokens": 0},
        })
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    client = _Client([conflicting, _Response("0.01", [_Choice()], "must-not-run")])

    with pytest.raises(run.BudgetExceeded, match="conflicting response route identity"):
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)

    assert client.calls == 1
    record = json.loads(raw.read_text())
    assert record["response_metadata"]["route_conflicts"] == {
        "model": ["z-ai/glm-4.7-flash", "wrong/model"],
    }


def test_recorder_retains_real_shaped_sdk_429_before_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import openai
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    body = {
        "error": {
            "code": 429,
            "message": "Provider rate limited the request",
            "metadata": {"provider_name": "DeepInfra", "retry_after": 2},
        },
        "choices": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "messages": [{"role": "user", "content": "secret"}],
        "headers": {"authorization": "Bearer secret"},
    }
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        json=body,
    )
    rate_limit = openai.RateLimitError("rate limit", response=response, body=body)
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    client = _Client([rate_limit, _Response("0.01", [_Choice()], "success")])
    run.CALL_CONTEXT.trace_cell = CELL
    try:
        result = run._oai.chat_completion_request(
            client, "z-ai/glm-4.7-flash", [{"role": "user", "content": "secret"}], [], None
        )
    finally:
        run.CALL_CONTEXT.trace_cell = None

    assert result.id == "success"
    records = [json.loads(line) for line in raw.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["attempt_status"] == "error"
    assert records[0]["billing_status"] == "not_reported"
    assert records[0]["billed_cost_usd"] is None
    assert records[0]["error"]["body"] == {
        "code": 429,
        "metadata": {"provider_name": "DeepInfra", "retry_after": 2},
    }
    serialized = json.dumps(records[0])
    assert '"messages"' not in serialized
    assert "Bearer" not in serialized
    summary = integrity.summarize_raw_responses(raw)
    assert summary["cost_complete"] is True
    assert summary["total_billed_cost_usd"] == 0.01


def test_returned_429_is_sanitized_and_retried_without_inventing_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    client = _Client([_ReturnedErrorResponse(), _Response("0.01", [_Choice()], "success")])
    run.CALL_CONTEXT.trace_cell = CELL
    try:
        result = run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)
    finally:
        run.CALL_CONTEXT.trace_cell = None

    assert result.id == "success"
    records = [json.loads(line) for line in raw.read_text().splitlines()]
    assert records[0]["attempt_status"] == "error"
    assert records[0]["billing_status"] == "not_reported"
    assert records[0]["raw_response"] is None
    assert records[0]["response_metadata"]["model"] == "z-ai/glm-4.7-flash"
    assert records[0]["response_metadata"]["provider"] == "DeepInfra"
    assert records[0]["response_metadata"]["finish_reasons"] == []
    assert records[0]["response_metadata"]["error"]["code"] == 429
    assert records[0]["error"]["body"]["metadata"]["provider_name"] == "DeepInfra"
    assert "private prompt" not in json.dumps(records[0])
    assert "private-key" not in json.dumps(records[0])
    assert "raw diagnostic" not in json.dumps(records[0])
    summary = integrity.summarize_raw_responses(raw)
    assert summary["attempt_count"] == 2
    assert summary["error_attempt_count"] == 1
    assert summary["successful_response_count"] == 1
    assert summary["cost_complete"] is True
    assert summary["total_billed_cost_usd"] == 0.01
    validation = integrity.validate_raw_response_summary(
        summary,
        model_id="z-ai/glm-4.7-flash",
        provider="deepinfra/bf16",
        response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.10,
        expected_cells=[CELL],
    )
    assert validation["ok"] is True


def test_sdk_429_nested_provider_identity_is_bound_to_success_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    body = {
        "error": {"code": 429, "metadata": {"provider_name": "WrongProvider"}},
        "choices": [],
        "usage": {"completion_tokens": 0},
    }
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    client = _Client([_sdk_rate_limit(body), _Response("0.01", [_Choice()], "success")])
    run.CALL_CONTEXT.trace_cell = CELL
    try:
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)
    finally:
        run.CALL_CONTEXT.trace_cell = None

    records = [json.loads(line) for line in raw.read_text().splitlines()]
    assert records[0]["provider"] == "WrongProvider"
    assert records[0]["response_metadata"]["provider"] == "WrongProvider"
    validation = integrity.validate_raw_response_summary(
        integrity.summarize_raw_responses(raw),
        model_id="z-ai/glm-4.7-flash", provider="deepinfra/bf16",
        response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.10, expected_cells=[CELL],
    )
    assert validation["ok"] is False
    assert "invalid raw response row 1: provider" in validation["errors"]


def test_returned_error_with_output_and_stop_has_unknown_billing_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    client = _Client([_ReturnedOutputErrorResponse(), _Response("0.01", [_Choice()], "must-not-run")])

    with pytest.raises(run.BudgetExceeded, match="billing outcome is unknown"):
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)

    assert client.calls == 1
    record = json.loads(raw.read_text())
    assert record["billing_status"] == "unknown"
    assert record["response_metadata"]["finish_reasons"] == ["stop"]
    assert "generated output" not in json.dumps(record)
    with pytest.raises(run.BudgetExceeded, match="billing outcome is unknown"):
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)
    assert client.calls == 1


def test_sdk_error_reported_cost_is_summed_and_enforces_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    body = {
        "error": {"code": 429, "metadata": {"provider_name": "DeepInfra"}},
        "choices": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.25},
    }
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.255)
    client = _Client([_sdk_rate_limit(body), _Response("0.01", [_Choice()], "success")])
    run.CALL_CONTEXT.trace_cell = CELL
    try:
        with pytest.raises(run.BudgetExceeded, match="cost ceiling exceeded"):
            run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)
    finally:
        run.CALL_CONTEXT.trace_cell = None

    assert client.calls == 2
    records = [json.loads(line) for line in raw.read_text().splitlines()]
    assert [record["billing_status"] for record in records] == ["reported", "reported"]
    assert [record["billed_cost_usd"] for record in records] == [0.25, "0.01"]
    summary = integrity.summarize_raw_responses(raw)
    assert summary["total_billed_cost_usd"] == 0.26
    validation = integrity.validate_raw_response_summary(
        summary,
        model_id="z-ai/glm-4.7-flash", provider="deepinfra/bf16",
        response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.255, expected_cells=[CELL],
    )
    assert validation["ok"] is False
    assert "raw response billed cost exceeds configured ceiling" in validation["errors"]


def test_error_attempt_rejects_present_wrong_response_route_identity(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    raw.write_text("\n".join(json.dumps(record) for record in [
        {
            "attempt_id": 1, "attempt_status": "error", "billing_status": "not_reported",
            "response_id": None, "requested_model": "z-ai/glm-4.7-flash",
            "requested_provider_tag": "deepinfra/bf16", "response_model": "wrong/model",
            "provider": "WrongProvider", "finish_reason": None, "usage": None,
            "billed_cost_usd": None, "trace_cell": CELL, "raw_response": None,
            "error": {"type": "ProviderResponseError", "status_code": 429, "body": {"code": 429}},
        },
        {
            "attempt_id": 2, "attempt_status": "response", "billing_status": "reported",
            "response_id": "success", "requested_model": "z-ai/glm-4.7-flash",
            "requested_provider_tag": "deepinfra/bf16", "response_model": "z-ai/glm-4.7-flash",
            "provider": "DeepInfra", "finish_reason": "stop", "usage": {"total_tokens": 10},
            "billed_cost_usd": 0.01, "trace_cell": CELL, "raw_response": {"id": "success"},
            "error": None,
        },
    ]) + "\n")

    validation = integrity.validate_raw_response_summary(
        integrity.summarize_raw_responses(raw),
        model_id="z-ai/glm-4.7-flash", provider="deepinfra/bf16",
        response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.10, expected_cells=[CELL],
    )

    assert validation["ok"] is False
    assert "invalid raw response row 1: response_model" in validation["errors"]
    assert "invalid raw response row 1: provider" in validation["errors"]


def test_preflight_uses_successful_response_cells_not_error_attempt_cells(tmp_path: Path) -> None:
    second = {**CELL, "user_task_id": "user_task_1"}
    config = {
        "model_id": "z-ai/glm-4.7-flash", "provider": "deepinfra/bf16",
        "response_model": "z-ai/glm-4.7-flash",
        "endpoint_revision": "z-ai/glm-4.7-flash-20260119", "max_cost_usd": 0.10,
        "expected_trace_cells": [CELL, second], "benchmark_version": "v1.2.2",
        "agentdojo_version": "0.1.35",
    }
    integrity.ensure_capture_lock(tmp_path, config)
    (tmp_path / "raw_responses.jsonl").write_text("\n".join(json.dumps(record) for record in [
        {
            "attempt_id": 1, "attempt_status": "response", "billing_status": "reported",
            "response_id": "success", "requested_model": config["model_id"],
            "requested_provider_tag": config["provider"], "response_model": config["response_model"],
            "provider": "DeepInfra", "finish_reason": "stop", "usage": {"total_tokens": 10},
            "billed_cost_usd": 0.01, "trace_cell": CELL,
        },
        {
            "attempt_id": 2, "attempt_status": "error", "billing_status": "not_reported",
            "response_id": None, "requested_model": config["model_id"],
            "requested_provider_tag": config["provider"], "response_model": config["response_model"],
            "provider": "DeepInfra", "finish_reason": None, "usage": None,
            "billed_cost_usd": None, "trace_cell": second,
            "error": {"type": "ProviderResponseError", "status_code": 429, "body": {"code": 429}},
        },
    ]) + "\n")
    for name, cell in (("first", CELL), ("second", second)):
        trace = _trace(cell)
        trace.update({"benchmark_version": "v1.2.2", "agentdojo_package_version": "0.1.35"})
        _write_trace(tmp_path / "agentdojo", name, trace)

    report = integrity.preflight_capture_artifacts(tmp_path, config)

    assert report["ok"] is False
    assert report["state"] == "partial"
    assert report["errors"] == ["raw and trace cells do not match"]


def test_unknown_transport_billing_latches_recorder_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    client = _Client(
        [ConnectionError("reset after send") for _ in range(6)]
        + [_Response("0.01", [_Choice()], "must-not-run")]
    )

    with pytest.raises(run.BudgetExceeded, match="billing outcome is unknown"):
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)
    assert client.calls == 1
    record = json.loads(raw.read_text())
    assert record["billing_status"] == "unknown"
    with pytest.raises(run.BudgetExceeded, match="billing outcome is unknown"):
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)
    assert client.calls == 1


def test_successful_trace_cannot_hide_error_only_call_ledger(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    raw.write_text(json.dumps({
        "attempt_id": 1,
        "attempt_status": "error",
        "billing_status": "not_reported",
        "response_id": None,
        "requested_model": "z-ai/glm-4.7-flash",
        "requested_provider_tag": "deepinfra/bf16",
        "response_model": None,
        "provider": None,
        "finish_reason": None,
        "usage": None,
        "billed_cost_usd": None,
        "trace_cell": CELL,
        "raw_response": None,
        "error": {"type": "RateLimitError", "status_code": 429, "body": {"error": {"code": 429}}},
    }) + "\n")
    _write_trace(tmp_path / "agentdojo", "complete-trace", _trace(CELL))

    trace_validation = integrity.validate_trace_corpus(tmp_path / "agentdojo", [CELL])

    validation = integrity.validate_raw_response_summary(
        integrity.summarize_raw_responses(raw),
        model_id="z-ai/glm-4.7-flash",
        provider="deepinfra/bf16",
        response_model="z-ai/glm-4.7-flash",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        max_cost_usd=0.10,
        expected_cells=[CELL],
    )

    assert trace_validation["ok"] is True
    assert validation["ok"] is False
    assert "raw responses do not cover every expected trace cell" in validation["errors"]


def test_recorder_records_empty_choice_attempt_before_retry_and_uses_decimal_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tenacity

    monkeypatch.setattr(tenacity, "wait_random_exponential", lambda **_: tenacity.wait_none())
    raw = tmp_path / "raw_responses.jsonl"
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    patched = run._oai.chat_completion_request
    client = _Client([_Response("0.06", [], "attempt-empty"), _Response("0.06", [_Choice()], "attempt-retry")])

    with pytest.raises(run.BudgetExceeded):
        patched(client, "z-ai/glm-4.7-flash", [], [], None)

    assert client.calls == 2
    records = [json.loads(line) for line in raw.read_text().splitlines()]
    assert [record["response_id"] for record in records] == ["attempt-empty", "attempt-retry"]
    assert [record["attempt_id"] for record in records] == [1, 2]
    assert records[0]["raw_response"] is None
    assert records[0]["error"] == {"type": "EmptyChoices", "status_code": None, "body": None}
    assert integrity.summarize_raw_responses(raw)["total_billed_cost_usd"] == 0.12


def test_recorder_refuses_resume_at_existing_decimal_budget(tmp_path: Path) -> None:
    raw = tmp_path / "raw_responses.jsonl"
    raw.write_text(json.dumps({
        "attempt_id": 1, "response_id": "old", "requested_model": "z-ai/glm-4.7-flash",
        "requested_provider_tag": "deepinfra/bf16", "response_model": "z-ai/glm-4.7-flash",
        "provider": "DeepInfra", "usage": {"total_tokens": 10, "cost": "0.10"},
        "billed_cost_usd": "0.10",
    }) + "\n")
    run.install_recorder(raw, "deepinfra/bf16", 0.0, 0.10)
    client = _Client([_Response("0.01", [_Choice()], "new")])

    with pytest.raises(run.BudgetExceeded):
        run._oai.chat_completion_request(client, "z-ai/glm-4.7-flash", [], [], None)

    assert client.calls == 0
