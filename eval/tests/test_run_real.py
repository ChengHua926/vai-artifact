from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from eval.agentdojo.evaluate import summarize_case_rows as summarize_agentdojo
from eval.run import (
    _require_linked_case_ids,
    _require_unique_ledger_rows,
    _validate_agentdojo_exact_matches,
    _validate_tau_exact_links,
    generate_evaluation_bundle,
)
from eval.tau.evaluate import summarize_case_rows as summarize_tau


REPO_ROOT = Path(__file__).resolve().parents[2]
TAU_ROOT_CANDIDATES = (
    REPO_ROOT.parent / "tau2-explore",
    REPO_ROOT.parent.parent / "tau2-explore",
)


def _tau_root() -> Path:
    return next(
        (candidate for candidate in TAU_ROOT_CANDIDATES if candidate.is_dir()),
        TAU_ROOT_CANDIDATES[0],
    )


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _verify_sums(directory: Path) -> None:
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, relative = line.split("  ", 1)
        assert hashlib.sha256((directory / relative).read_bytes()).hexdigest() == digest


def test_real_paper_main_bundle_is_complete_folded_hashed_and_reproducible(
    tmp_path: Path,
) -> None:
    output = tmp_path / "paper_main_v1"

    first_index = generate_evaluation_bundle(output, tau_root=_tau_root())
    first_bytes = _tree_bytes(output)

    assert first_index["benchmarks"]["agentdojo"]["ledger_counts"] == {
        "calls": 7446,
        "cases": 2162,
        "grader_contracts": 35,
        "promises": 10,
        "verdicts": 4845,
    }
    assert first_index["benchmarks"]["tau"]["ledger_counts"] == {
        "calls": 2333,
        "cases": 328,
        "effect_contracts": 19,
        "effects": 900,
        "fires": 74,
        "monitor_manifest": 1,
    }
    assert first_index["benchmarks"]["tau"]["monitor_parity_mismatch_count"] == 0

    for benchmark, ledger, path in (
        ("agentdojo", "calls", output / "agentdojo" / "calls.jsonl"),
        ("agentdojo", "verdicts", output / "agentdojo" / "verdicts.jsonl"),
        (
            "agentdojo",
            "grader_contracts",
            output / "agentdojo" / "grader_contracts.jsonl",
        ),
        ("agentdojo", "promises", output / "agentdojo" / "promises.jsonl"),
        ("tau", "calls", output / "tau" / "calls.jsonl"),
        ("tau", "effects", output / "tau" / "effects.jsonl"),
        ("tau", "fires", output / "tau" / "fires.jsonl"),
        ("tau", "effect_contracts", output / "tau" / "effect_contracts.jsonl"),
    ):
        corrupted = _rows(path)
        corrupted[-1] = dict(corrupted[0])
        with pytest.raises(ValueError, match=f"duplicate {benchmark} {ledger}"):
            _require_unique_ledger_rows(benchmark, ledger, corrupted)

    agentdojo_cases = _rows(output / "agentdojo" / "cases.jsonl")
    agentdojo_calls = _rows(output / "agentdojo" / "calls.jsonl")
    agentdojo_verdicts = _rows(output / "agentdojo" / "verdicts.jsonl")
    agentdojo_case = next(
        row
        for row in agentdojo_cases
        if row["case_id"]
        == "case_1a440006c839524fe0193267ece6761792ece15dd6b9939ac42090798aaa694f"
    )
    agentdojo_case_calls = [
        row
        for row in agentdojo_calls
        if row["case_id"] == agentdojo_case["case_id"]
    ]
    agentdojo_case_verdicts = [
        row
        for row in agentdojo_verdicts
        if row["case_id"] == agentdojo_case["case_id"]
    ]

    swapped_case = copy.deepcopy(agentdojo_case)
    swapped_case["exact_matches"][0]["call"]["seq"] = 4
    with pytest.raises(ValueError, match="same causal event"):
        _validate_agentdojo_exact_matches(
            [swapped_case], agentdojo_case_calls, agentdojo_case_verdicts
        )

    forged_call_evidence = copy.deepcopy(agentdojo_case)
    forged_call_evidence["exact_matches"][0]["call"]["execution_status"] = "forged"
    with pytest.raises(ValueError, match="call evidence disagrees"):
        _validate_agentdojo_exact_matches(
            [forged_call_evidence], agentdojo_case_calls, agentdojo_case_verdicts
        )

    missing_model = dict(agentdojo_case_calls[0])
    del missing_model["model_id"]
    with pytest.raises(ValueError, match="agentdojo parent identity mismatch"):
        _require_linked_case_ids(
            "agentdojo", [agentdojo_case], {"calls": [missing_model]}
        )

    tau_cases = _rows(output / "tau" / "cases.jsonl")
    tau_calls = _rows(output / "tau" / "calls.jsonl")
    tau_effects = _rows(output / "tau" / "effects.jsonl")
    tau_fires = _rows(output / "tau" / "fires.jsonl")
    tau_contracts = _rows(output / "tau" / "effect_contracts.jsonl")
    tau_monitor = _rows(output / "tau" / "monitor_manifest.jsonl")[0]
    tau_case = next(
        row
        for row in tau_cases
        if row["case_id"]
        == "case_3ef2beba19cbf895ec2f08467fc66ad540519d042eb712ca221c4f996956df5d"
    )
    tau_case_calls = [row for row in tau_calls if row["case_id"] == tau_case["case_id"]]
    tau_case_effects = [
        row for row in tau_effects if row["case_id"] == tau_case["case_id"]
    ]
    tau_case_fires = [
        row for row in tau_fires if row["case_id"] == tau_case["case_id"]
    ]
    exact_fire_index = next(
        index for index, row in enumerate(tau_case_fires) if row["exact_links"]
    )

    forged_projection_case = copy.deepcopy(tau_case)
    forged_projection_case["exact_links"][0]["protected_projection"] = "forged"
    forged_projection_fires = copy.deepcopy(tau_case_fires)
    forged_projection_fires[exact_fire_index]["exact_links"][0][
        "protected_projection"
    ] = "forged"
    with pytest.raises(ValueError, match="protected projection"):
        _validate_tau_exact_links(
            [forged_projection_case],
            tau_case_calls,
            tau_case_effects,
            forged_projection_fires,
            tau_contracts,
            tau_monitor["implementation"],
        )

    omitted_case_link = copy.deepcopy(tau_case)
    omitted_case_link["exact_links"] = []
    with pytest.raises(ValueError, match="case exact links do not equal"):
        _validate_tau_exact_links(
            [omitted_case_link],
            tau_case_calls,
            tau_case_effects,
            tau_case_fires,
            tau_contracts,
            tau_monitor["implementation"],
        )

    forged_effects = copy.deepcopy(tau_case_effects)
    effect_path = tau_case_fires[exact_fire_index]["exact_links"][0]["effect_paths"][0]
    effect = next(row for row in forged_effects if row["path"] == effect_path)
    effect["writer_tool"] = "forged"
    with pytest.raises(ValueError, match="written effect"):
        _validate_tau_exact_links(
            [tau_case],
            tau_case_calls,
            forged_effects,
            tau_case_fires,
            tau_contracts,
            tau_monitor["implementation"],
        )

    duplicate_path_case = copy.deepcopy(tau_case)
    duplicate_path_case["exact_links"][0]["effect_paths"].append(effect_path)
    duplicate_path_fires = copy.deepcopy(tau_case_fires)
    duplicate_path_fires[exact_fire_index]["exact_links"][0]["effect_paths"].append(
        effect_path
    )
    with pytest.raises(ValueError, match="duplicate effect path"):
        _validate_tau_exact_links(
            [duplicate_path_case],
            tau_case_calls,
            tau_case_effects,
            duplicate_path_fires,
            tau_contracts,
            tau_monitor["implementation"],
        )

    missing_domain = dict(tau_case_effects[0])
    del missing_domain["domain"]
    with pytest.raises(ValueError, match="tau parent identity mismatch"):
        _require_linked_case_ids("tau", [tau_case], {"effects": [missing_domain]})

    missing_trial = dict(tau_case_fires[0])
    del missing_trial["trial"]
    with pytest.raises(ValueError, match="tau parent identity mismatch"):
        _require_linked_case_ids("tau", [tau_case], {"fires": [missing_trial]})

    assert json.loads((output / "agentdojo" / "summary.json").read_bytes()) == (
        summarize_agentdojo(agentdojo_cases)
    )
    assert json.loads((output / "tau" / "summary.json").read_bytes()) == (
        summarize_tau(tau_cases)
    )
    _verify_sums(output / "agentdojo")
    _verify_sums(output / "tau")
    _verify_sums(output)
    assert b'"/Users/' not in b"\n".join(first_bytes.values())

    second_index = generate_evaluation_bundle(output, tau_root=_tau_root())

    assert second_index == first_index
    assert _tree_bytes(output) == first_bytes
