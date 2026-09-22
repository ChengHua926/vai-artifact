from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from eval.dataset import Cohort
from eval.agentdojo.evaluate import summarize_case_rows as summarize_agentdojo
from eval.dataset import EvaluationCohort
from eval.run import (
    _write_benchmark_artifact,
    _write_verified_evaluation_bundle,
    _write_artifact_tree,
    generate_benchmark_bundle,
    generate_evaluation_bundle,
)
from eval.tau.evaluate import summarize_case_rows as summarize_tau


OUTER_SHA256 = "0" * 64


def _cohort(tmp_path: Path) -> EvaluationCohort:
    return EvaluationCohort(
        cohort=Cohort(
            cohort_id="paper_main_v1",
            lock_path=tmp_path / "cohort.lock.json",
            outer_manifest_path=tmp_path / "capture_manifest.json",
            accepted_shards=(),
        ),
        outer_sha256=OUTER_SHA256,
    )


def _agentdojo_result() -> dict:
    return {
        "case_rows": [],
        "call_rows": [{"case_id": "ad-case", "seq": 2, "tool": "send_email"}],
        "verdict_rows": [{"case_id": "ad-case", "seq": 2, "violated": True}],
        "grader_contracts": [{"suite": "workspace", "injection_task_id": "0"}],
        "promise_manifest": [{"promise_id": "promise-1", "tool": "send_email"}],
        "summary": summarize_agentdojo([]),
    }


def _tau_result() -> dict:
    return {
        "case_rows": [],
        "call_rows": [{"case_id": "tau-case", "seq": 1, "tool": "get_user_details"}],
        "effect_rows": [{"case_id": "tau-case", "path": ["agent", "users", "1"]}],
        "fire_rows": [{"case_id": "tau-case", "seq": 1, "arm": "auth_first"}],
        "effect_contracts": [{"arm": "auth_first", "exact_status": "never_exact"}],
        "monitor_manifest": {"implementation": "compiled_tau_monitor", "sources": []},
        "monitor_parity_mismatches": [],
        "summary": summarize_tau([]),
    }


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_sha256sums(directory: Path) -> None:
    lines = (directory / "SHA256SUMS").read_text().splitlines()
    assert lines
    for line in lines:
        digest, relative = line.split("  ", 1)
        assert hashlib.sha256((directory / relative).read_bytes()).hexdigest() == digest


def test_write_bundle_emits_portable_ledgers_index_and_complete_hash_chain(
    tmp_path: Path,
) -> None:
    output = tmp_path / "results"

    index = _write_artifact_tree(
        output,
        evaluation_cohort=_cohort(tmp_path),
        agentdojo_result=_agentdojo_result(),
        tau_result=_tau_result(),
    )

    expected = {
        "SHA256SUMS",
        "index.json",
        "agentdojo/SHA256SUMS",
        "agentdojo/calls.jsonl",
        "agentdojo/cases.jsonl",
        "agentdojo/grader_contracts.jsonl",
        "agentdojo/promises.jsonl",
        "agentdojo/summary.json",
        "agentdojo/verdicts.jsonl",
        "tau/SHA256SUMS",
        "tau/calls.jsonl",
        "tau/cases.jsonl",
        "tau/effect_contracts.jsonl",
        "tau/effects.jsonl",
        "tau/fires.jsonl",
        "tau/monitor_manifest.jsonl",
        "tau/summary.json",
    }
    assert set(_tree_bytes(output)) == expected
    assert json.loads((output / "index.json").read_bytes()) == index
    assert index["cohort"] == {
        "cohort_id": "paper_main_v1",
        "outer_sha256": OUTER_SHA256,
    }
    assert index["benchmarks"]["agentdojo"]["ledger_counts"] == {
        "calls": 1,
        "cases": 0,
        "grader_contracts": 1,
        "promises": 1,
        "verdicts": 1,
    }
    assert index["benchmarks"]["tau"]["ledger_counts"] == {
        "calls": 1,
        "cases": 0,
        "effect_contracts": 1,
        "effects": 1,
        "fires": 1,
        "monitor_manifest": 1,
    }
    for benchmark in ("agentdojo", "tau"):
        record = index["benchmarks"][benchmark]
        assert record["summary_sha256"] == record["artifact_hashes"]["summary.json"]
        assert record["sha256sums_sha256"] == hashlib.sha256(
            (output / benchmark / "SHA256SUMS").read_bytes()
        ).hexdigest()
    assert index["evaluator_sources"]["agentdojo"]
    assert index["evaluator_sources"]["tau"]
    agentdojo_sources = {
        row["path"] for row in index["evaluator_sources"]["agentdojo"]
    }
    tau_sources = {row["path"] for row in index["evaluator_sources"]["tau"]}
    assert "packages/sdk/aa_sdk/__init__.py" in agentdojo_sources
    assert (
        "packages/commons/aa_commons/predicates/egress_within_allowlist.py"
        in agentdojo_sources
    )
    assert "eval/tau/trace_adapter.py" in tau_sources
    assert "packages/commons/aa_commons/trace.py" in tau_sources
    assert index["bundle_runner_source"]["path"] == "eval/run.py"
    assert str(tmp_path) not in json.dumps(index)
    _assert_sha256sums(output / "agentdojo")
    _assert_sha256sums(output / "tau")
    _assert_sha256sums(output)


def test_write_bundle_rerun_is_byte_identical(tmp_path: Path) -> None:
    output = tmp_path / "results"
    kwargs = {
        "evaluation_cohort": _cohort(tmp_path),
        "agentdojo_result": _agentdojo_result(),
        "tau_result": _tau_result(),
    }
    _write_artifact_tree(output, **kwargs)
    before = _tree_bytes(output)

    _write_artifact_tree(output, **kwargs)

    assert _tree_bytes(output) == before


def test_consolidated_root_preserves_static_dataset_entries_and_rejects_managed_stale_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eval.run as run

    output = tmp_path / "paper_main_v1"
    static_files = {
        "MODEL_SELECTION.md": b"model selection\n",
        "MIGRATION.md": b"migration\n",
        "README.md": b"dataset\n",
        "baseline.json": b"{}\n",
        "cohort.lock.json": b"{}\n",
        "dataset.json": b'{"archive":null}\n',
        "seal/current/capture_manifest.json": b"{}\n",
        "corpus/accepted/evidence.json": b"{}\n",
    }
    for relative, content in static_files.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(run, "PAPER_MAIN_V1_ROOT", output)
    kwargs = {
        "evaluation_cohort": _cohort(tmp_path),
        "agentdojo_result": _agentdojo_result(),
        "tau_result": _tau_result(),
    }

    _write_artifact_tree(output, **kwargs)

    assert {
        relative: (output / relative).read_bytes() for relative in static_files
    } == static_files
    stale = output / "agentdojo" / "stale.json"
    stale.write_text("do not remove")
    with pytest.raises(ValueError, match="unexpected output artifact"):
        _write_artifact_tree(output, **kwargs)
    assert stale.read_text() == "do not remove"


@pytest.mark.parametrize(
    ("benchmark", "result", "expected"),
    [
        (
            "agentdojo",
            _agentdojo_result,
            {
                "SHA256SUMS",
                "calls.jsonl",
                "cases.jsonl",
                "grader_contracts.jsonl",
                "promises.jsonl",
                "summary.json",
                "verdicts.jsonl",
            },
        ),
        (
            "tau",
            _tau_result,
            {
                "SHA256SUMS",
                "calls.jsonl",
                "cases.jsonl",
                "effect_contracts.jsonl",
                "effects.jsonl",
                "fires.jsonl",
                "monitor_manifest.jsonl",
                "summary.json",
            },
        ),
    ],
)
def test_benchmark_mode_writes_only_selected_child_bundle(
    tmp_path: Path, benchmark: str, result, expected: set[str]
) -> None:
    output = tmp_path / "results"

    record = _write_benchmark_artifact(output, benchmark=benchmark, result=result())

    assert set(_tree_bytes(output / benchmark)) == expected
    assert not (output / "index.json").exists()
    assert record["ledger_counts"]["cases"] == 0
    _assert_sha256sums(output / benchmark)


@pytest.mark.parametrize("benchmark", ["agentdojo", "tau"])
def test_generate_benchmark_mode_evaluates_only_selected_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, benchmark: str
) -> None:
    import eval.run as run

    cohort = _cohort(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(run, "load_paper_main_v1", lambda: cohort)

    def fake_agentdojo(received: EvaluationCohort) -> dict:
        assert received is cohort
        calls.append("agentdojo")
        return _agentdojo_result()

    def fake_tau(received: EvaluationCohort, *, tau_root: Path | None = None) -> dict:
        assert received is cohort
        assert tau_root == tmp_path / "tau-root"
        calls.append("tau")
        return _tau_result()

    monkeypatch.setattr(run, "evaluate_agentdojo", fake_agentdojo)
    monkeypatch.setattr(run, "evaluate_tau", fake_tau)
    monkeypatch.setattr(
        run,
        "_write_verified_benchmark_bundle",
        lambda _output, *, evaluation_cohort, benchmark, result: {"mode": benchmark},
    )

    record = generate_benchmark_bundle(
        tmp_path / "results",
        benchmark=benchmark,
        tau_root=tmp_path / "tau-root",
    )

    assert record == {"mode": benchmark}
    assert calls == [benchmark]


@pytest.mark.parametrize("stale_relative", ["stale.json", "agentdojo/stale.json"])
def test_write_bundle_rejects_stale_files_without_removing_them(
    tmp_path: Path, stale_relative: str
) -> None:
    output = tmp_path / "results"
    kwargs = {
        "evaluation_cohort": _cohort(tmp_path),
        "agentdojo_result": _agentdojo_result(),
        "tau_result": _tau_result(),
    }
    _write_artifact_tree(output, **kwargs)
    before = _tree_bytes(output)
    stale = output / stale_relative
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("do not remove")

    with pytest.raises(ValueError, match="unexpected output artifact"):
        _write_artifact_tree(output, **kwargs)

    assert stale.read_text() == "do not remove"
    assert {
        key: value for key, value in _tree_bytes(output).items() if key != stale_relative
    } == before


def test_generate_uses_one_fresh_verified_cohort_for_both_evaluators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eval.run as run

    cohort = _cohort(tmp_path)
    tau_root = tmp_path / "tau-checkout"

    monkeypatch.setattr(run, "load_paper_main_v1", lambda: cohort)

    def fake_agentdojo(received: EvaluationCohort) -> dict:
        assert received is cohort
        return _agentdojo_result()

    def fake_tau(received: EvaluationCohort, *, tau_root: Path | None = None) -> dict:
        assert received is cohort
        assert tau_root == tmp_path / "tau-checkout"
        return _tau_result()

    monkeypatch.setattr(run, "evaluate_agentdojo", fake_agentdojo)
    monkeypatch.setattr(run, "evaluate_tau", fake_tau)

    def fake_writer(
        output_dir: Path,
        *,
        evaluation_cohort: EvaluationCohort,
        agentdojo_result: dict,
        tau_result: dict,
    ) -> dict:
        assert output_dir == tmp_path / "results"
        assert evaluation_cohort is cohort
        assert agentdojo_result == _agentdojo_result()
        assert tau_result == _tau_result()
        return {"cohort": {"outer_sha256": evaluation_cohort.outer_sha256}}

    monkeypatch.setattr(run, "_write_verified_evaluation_bundle", fake_writer)

    index = generate_evaluation_bundle(tmp_path / "results", tau_root=tau_root)

    assert index["cohort"]["outer_sha256"] == OUTER_SHA256


def test_generate_rejects_evaluator_source_changes_during_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eval.run as run

    cohort = _cohort(tmp_path)
    changed = False
    evaluator_calls: list[str] = []

    def fake_sources(benchmark: str) -> list[dict[str, str]]:
        return [
            {
                "path": f"eval/{benchmark}/evaluate.py",
                "sha256": ("1" if changed else "0") * 64,
            }
        ]

    def fake_agentdojo(received: EvaluationCohort) -> dict:
        nonlocal changed
        assert received is cohort
        evaluator_calls.append("agentdojo")
        changed = True
        return _agentdojo_result()

    def fake_tau(received: EvaluationCohort, tau_root: Path | None = None) -> dict:
        assert received is cohort
        evaluator_calls.append("tau")
        return _tau_result()

    monkeypatch.setattr(run, "load_paper_main_v1", lambda: cohort)
    monkeypatch.setattr(run, "_source_manifest", fake_sources)
    monkeypatch.setattr(run, "_IMPORTED_SOURCE_PROVENANCE", run._source_provenance())
    monkeypatch.setattr(run, "evaluate_agentdojo", fake_agentdojo)
    monkeypatch.setattr(run, "evaluate_tau", fake_tau)

    with pytest.raises(ValueError, match="source files changed during evaluation"):
        generate_evaluation_bundle(tmp_path / "results")
    assert evaluator_calls == ["agentdojo", "tau"]


@pytest.mark.parametrize("benchmark", ["agentdojo", "tau"])
def test_write_bundle_rejects_a_summary_not_equal_to_the_pure_fold(
    tmp_path: Path, benchmark: str
) -> None:
    agentdojo = _agentdojo_result()
    tau = _tau_result()
    target = agentdojo if benchmark == "agentdojo" else tau
    target["summary"] = {"wrong": True}

    with pytest.raises(ValueError, match=f"{benchmark} summary is not the pure case fold"):
        _write_artifact_tree(
            tmp_path / "results",
            evaluation_cohort=_cohort(tmp_path),
            agentdojo_result=agentdojo,
            tau_result=tau,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cwd", "workspace", "forbidden provenance key"),
        ("manual_label", "caught", "forbidden provenance key"),
        ("manual_review", "caught", "forbidden provenance key"),
        ("human_label", "caught", "forbidden provenance key"),
        ("llm_label", "missed", "forbidden provenance key"),
        ("LLM_Judge", "missed", "forbidden provenance key"),
        ("judge_model", "model", "forbidden provenance key"),
        ("host_id", "machine-1", "forbidden provenance key"),
        ("note", "/tmp/evaluation", "absolute path"),
        ("note", r"C:\\evaluation\\result", "absolute path"),
        ("note", "FILE:///tmp/evaluation", "absolute path"),
        ("note", "file:/tmp/evaluation", "absolute path"),
    ],
)
def test_write_bundle_rejects_host_manual_llm_and_absolute_path_provenance(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    agentdojo = _agentdojo_result()
    agentdojo["call_rows"][0][field] = value

    with pytest.raises(ValueError, match=message):
        _write_artifact_tree(
            tmp_path / "results",
            evaluation_cohort=_cohort(tmp_path),
            agentdojo_result=agentdojo,
            tau_result=_tau_result(),
        )


def test_internal_verified_writer_rejects_a_forged_paper_main_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eval.run as run

    verified = _cohort(tmp_path)
    forged = EvaluationCohort(cohort=verified.cohort, outer_sha256="1" * 64)
    monkeypatch.setattr(run, "load_paper_main_v1", lambda: verified)

    with pytest.raises(ValueError, match="does not match freshly verified paper_main_v1"):
        _write_verified_evaluation_bundle(
            tmp_path / "results",
            evaluation_cohort=forged,
            agentdojo_result=_agentdojo_result(),
            tau_result=_tau_result(),
        )


def test_internal_verified_writer_rejects_paper_main_ledger_count_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eval.run as run

    verified = _cohort(tmp_path)
    monkeypatch.setattr(run, "load_paper_main_v1", lambda: verified)

    with pytest.raises(ValueError, match="agentdojo ledger count"):
        _write_verified_evaluation_bundle(
            tmp_path / "results",
            evaluation_cohort=verified,
            agentdojo_result=_agentdojo_result(),
            tau_result=_tau_result(),
        )


def test_case_link_validation_rejects_duplicates_and_dangling_rows() -> None:
    import eval.run as run

    with pytest.raises(ValueError, match="duplicate agentdojo case_id"):
        run._require_linked_case_ids(
            "agentdojo",
            [
                {"case_id": "case-a", "case_key": "key-a"},
                {"case_id": "case-a", "case_key": "key-b"},
            ],
            {"calls": []},
        )
    with pytest.raises(ValueError, match="unknown agentdojo case_id"):
        run._require_linked_case_ids(
            "agentdojo",
            [{"case_id": "case-a", "case_key": "key-a"}],
            {"calls": [{"case_id": "case-b"}]},
        )


def test_case_membership_rejects_wrong_model_for_a_sealed_case_id() -> None:
    import eval.run as run

    expected = [
        {
            "case_id": "case-a",
            "case_key": "key-a",
            "model_id": "glm47",
            "cohort_outer_sha256": OUTER_SHA256,
        }
    ]
    actual = [{**expected[0], "model_id": "qwen3_30b"}]

    with pytest.raises(ValueError, match="agentdojo case membership mismatch"):
        run._require_case_membership("agentdojo", actual, expected, OUTER_SHA256)


@pytest.mark.parametrize(
    ("benchmark", "ledger", "row"),
    [
        ("agentdojo", "calls", {"case_id": "case-a", "seq": 1}),
        (
            "agentdojo",
            "verdicts",
            {"case_id": "case-a", "promise_id": "promise-1"},
        ),
        (
            "agentdojo",
            "grader_contracts",
            {"suite": "banking", "injection_task_id": "injection_task_0"},
        ),
        ("agentdojo", "promises", {"promise_id": "promise-1"}),
        ("tau", "calls", {"case_id": "case-a", "seq": 1}),
        ("tau", "effects", {"case_id": "case-a", "path": ["agent", "x"]}),
        (
            "tau",
            "fires",
            {
                "case_id": "case-a",
                "seq": 1,
                "predicate": "scope",
                "arm": "auth_first",
                "targets": ["x"],
                "detail": "d",
            },
        ),
        ("tau", "effect_contracts", {"arm": "auth_first"}),
    ],
)
def test_non_case_ledgers_reject_duplicate_natural_keys(
    benchmark: str, ledger: str, row: dict
) -> None:
    import eval.run as run

    with pytest.raises(ValueError, match=f"duplicate {benchmark} {ledger}"):
        run._require_unique_ledger_rows(benchmark, ledger, [row, dict(row)])


def test_linked_rows_reject_denormalized_parent_identity_drift() -> None:
    import eval.run as run

    case = {
        "case_id": "case-a",
        "case_key": "key-a",
        "model_id": "glm47",
        "suite": "banking",
        "user_task_id": "user_task_0",
        "injection_task_id": "injection_task_0",
    }
    linked = {**case, "model_id": "qwen3_30b", "seq": 1}

    with pytest.raises(ValueError, match="agentdojo parent identity mismatch"):
        run._require_linked_case_ids("agentdojo", [case], {"calls": [linked]})

    missing_model = dict(linked)
    del missing_model["model_id"]
    with pytest.raises(ValueError, match="agentdojo parent identity mismatch"):
        run._require_linked_case_ids(
            "agentdojo", [case], {"calls": [missing_model]}
        )

    tau_case = {
        "case_id": "case-tau",
        "case_key": "key-tau",
        "model_id": "glm47",
        "domain": "airline",
        "task_id": "1",
        "trial": 0,
    }
    tau_effect = {**tau_case, "case_key": "forged", "path": ["agent", "x"]}
    with pytest.raises(ValueError, match="tau parent identity mismatch"):
        run._require_linked_case_ids("tau", [tau_case], {"effects": [tau_effect]})

    missing_domain = dict(tau_effect)
    missing_domain["case_key"] = tau_case["case_key"]
    del missing_domain["domain"]
    with pytest.raises(ValueError, match="tau parent identity mismatch"):
        run._require_linked_case_ids(
            "tau", [tau_case], {"effects": [missing_domain]}
        )


def test_ledger_references_and_exact_manifests_fail_closed() -> None:
    import eval.run as run

    with pytest.raises(ValueError, match="unknown promise_id"):
        run._require_field_references(
            "agentdojo verdicts", [{"promise_id": "promise-2"}], "promise_id", {"promise-1"}
        )
    with pytest.raises(ValueError, match="unknown arm"):
        run._require_field_references(
            "tau fires", [{"arm": "made_up"}], "arm", {"auth_first"}
        )
    with pytest.raises(ValueError, match="monitor manifest mismatch"):
        run._require_exact_manifest(
            "monitor manifest", [{}], [{"implementation": "compiled_tau_monitor"}]
        )


def test_agentdojo_exact_match_requires_identical_call_and_verdict_event() -> None:
    import eval.run as run

    calls = [
        {
            "case_id": "case-a",
            "seq": seq,
            "tool": "send_money",
            "args": {"recipient": "attacker"},
            "execution_status": "successful",
            "replay_status": "successful",
        }
        for seq in (2, 4)
    ]
    verdict = {
        "case_id": "case-a",
        "promise_id": "promise-1",
        "suite": "banking",
        "tool": "send_money",
        "recipient_key": "recipient",
        "violated": True,
        "seq": 2,
        "reason": "egress send_money to non-allowlisted recipient 'attacker'",
        "raw_target": "attacker",
        "parse_error": None,
    }
    call_evidence = {
        "seq": 2,
        "tool": "send_money",
        "recipient_key": "recipient",
        "raw_target": "attacker",
        "execution_status": "successful",
        "replay_status": "successful",
    }
    verdict_evidence = {
        key: verdict[key]
        for key in (
            "promise_id",
            "suite",
            "tool",
            "recipient_key",
            "violated",
            "seq",
            "reason",
            "raw_target",
            "parse_error",
        )
    }

    swapped = [
        {
            "case_id": "case-a",
            "exact_matches": [
                {
                    "call": {**call_evidence, "seq": 4},
                    "verdict": verdict_evidence,
                }
            ],
        }
    ]
    with pytest.raises(ValueError, match="same causal event"):
        run._validate_agentdojo_exact_matches(swapped, calls, [verdict])

    forged = [
        {
            "case_id": "case-a",
            "exact_matches": [
                {
                    "call": {**call_evidence, "execution_status": "forged"},
                    "verdict": verdict_evidence,
                }
            ],
        }
    ]
    with pytest.raises(ValueError, match="call evidence disagrees"):
        run._validate_agentdojo_exact_matches(forged, calls, [verdict])


def test_tau_exact_links_require_contract_writer_unique_paths_and_case_equality() -> None:
    import copy
    import eval.run as run

    link = {
        "arm": "arm-a",
        "seq": 1,
        "tool": "write_tool",
        "protected_projection": "projection-a",
        "effect_paths": [["agent", "value"]],
    }
    cases = [{"case_id": "case-a", "exact_links": [link]}]
    calls = [{"case_id": "case-a", "seq": 1, "tool": "write_tool"}]
    effects = [
        {
            "case_id": "case-a",
            "path": ["agent", "value"],
            "writer_seq": 1,
            "writer_tool": "write_tool",
        }
    ]
    fires = [
        {
            "case_id": "case-a",
            "seq": 1,
            "tool": "write_tool",
            "arm": "arm-a",
            "implementation": "compiled_tau_monitor",
            "exact_links": [link],
        }
    ]
    contracts = [{"arm": "arm-a", "protected_projection": "projection-a"}]

    forged_projection = copy.deepcopy(fires)
    forged_projection[0]["exact_links"][0]["protected_projection"] = "forged"
    with pytest.raises(ValueError, match="protected projection"):
        run._validate_tau_exact_links(
            cases, calls, effects, forged_projection, contracts, "compiled_tau_monitor"
        )

    with pytest.raises(ValueError, match="case exact links do not equal"):
        run._validate_tau_exact_links(
            [{"case_id": "case-a", "exact_links": []}],
            calls,
            effects,
            fires,
            contracts,
            "compiled_tau_monitor",
        )

    forged_writer = copy.deepcopy(effects)
    forged_writer[0]["writer_tool"] = "other_tool"
    with pytest.raises(ValueError, match="written effect"):
        run._validate_tau_exact_links(
            cases, calls, forged_writer, fires, contracts, "compiled_tau_monitor"
        )

    duplicate_path = copy.deepcopy(fires)
    duplicate_path[0]["exact_links"][0]["effect_paths"].append(
        ["agent", "value"]
    )
    with pytest.raises(ValueError, match="duplicate effect path"):
        run._validate_tau_exact_links(
            cases, calls, effects, duplicate_path, contracts, "compiled_tau_monitor"
        )
