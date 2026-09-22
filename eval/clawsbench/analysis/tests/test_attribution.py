from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest


ANALYSIS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ANALYSIS_DIR))
pytestmark = pytest.mark.skipif(
    not (ANALYSIS_DIR / "coverage.json").is_file(),
    reason="generated local ClawsBench coverage is unavailable",
)


def test_reviewed_attribution_reconciles_frozen_rows_and_fires(tmp_path: Path) -> None:
    import make_figures

    coverage = json.loads((ANALYSIS_DIR / "coverage.json").read_text())
    attribution = json.loads((ANALYSIS_DIR / "attribution.json").read_text())
    summary = make_figures.build(coverage, attribution, tmp_path)

    assert summary["task_count"] == 60
    assert summary["nonfull_count"] == 30
    assert summary["fire_count"] == 338
    assert summary["valid_fire_counts"] == {
        "confirmed_violation": 338,
        "overfire": 0,
        "unclear": 0,
        "under_verification": 0,
    }
    assert summary["aligned_nonfull_count"] == 0
    assert summary["literal_nonfull_overlap"] == {
        "safety": 1,
        "aap5": 4,
        "union": 5,
    }
    assert {row["task"] for row in attribution["tasks"]} == {
        row["task"] for row in coverage
    }
    assert {row["task"]: row["reward"] for row in attribution["tasks"]} == {
        row["task"]: row["reward"] for row in coverage
    }
    frozen_fires = [fire["fire_id"] for row in coverage for fire in row["fires"]]
    assert sorted(summary["reviewed_fire_ids"]) == sorted(frozen_fires)
    assert len(summary["reviewed_fire_ids"]) == len(set(summary["reviewed_fire_ids"]))


def test_build_writes_complete_reference_and_deterministic_figures(tmp_path: Path) -> None:
    import make_figures

    coverage = json.loads((ANALYSIS_DIR / "coverage.json").read_text())
    attribution = json.loads((ANALYSIS_DIR / "attribution.json").read_text())
    output = tmp_path / "writeup"
    first = make_figures.build(coverage, attribution, output)
    reference = output / "clawsbench_reference.md"

    assert first["candidate_status_counts"] == {
        "passed": 3664,
        "fired": 365,
        "rejected": 0,
        "unsupported": 26,
    }
    assert first["outcome_support_counts"] == {
        "supported_nonfull": 28,
        "false_failure": 1,
        "unclear": 1,
        "not_applicable": 30,
    }
    assert first["primary_miss_counts"] == {
        "all_candidates_compliant": 5,
        "competence_or_underaction": 9,
        "detector_miss": 0,
        "no_relevant_action": 8,
        "outside_registered_surface": 7,
        "unsupported_evidence": 1,
    }
    assert first["miss_counts"]["verifier_mismatch"] == 1
    assert first["miss_counts"]["verifier_false_failure"] == 1
    assert first["full_reward_fire_disposition"] == {
        "benchmark_oversight": 7,
        "under_verification": 0,
        "overfire": 0,
        "unclear": 0,
    }
    assert first["evaluated_candidate_count"] == 4029
    assert first["nonpass_candidate_count"] == 391
    assert {row["task"] for row in first["raw_to_aligned_rows"]} == {
        "email-workflow-event-rsvp",
        "gdoc-search-keyword-index",
        "slack-channel-reorg",
        "slack-reaction-weekly-leaderboard",
        "stripe-least-privilege-charge",
    }
    assert sum(row["tasks"] for row in first["family_counts"].values()) == 60
    assert sum(row["candidates"] for row in first["family_counts"].values()) == 4055
    assert sum(row["actions"] for row in first["family_counts"].values()) == 3909
    assert sum(row["fires"] for row in first["family_counts"].values()) == 338
    assert sum(row["overlap"] for row in first["family_counts"].values()) == 5
    assert sum(row["valid"] for row in first["family_counts"].values()) == 5
    assert sum(row["aligned"] for row in first["family_counts"].values()) == 0
    text = reference.read_text()
    assert "Sealed denominator: 30 non-full tasks" in text
    assert "## Aggregate counts" in text
    assert "Actions: 3,909" in text
    assert "This is the scored agent-phase action count, not the raw action-log count." in text
    assert "| identical_call_cap | 3909 | 336 |" in text
    assert "| under_verification | 0 |" in text
    assert "## Raw-to-aligned composition" in text
    assert "## Full-reward fires" in text
    assert "## Complete non-pass candidate table" in text
    assert "| evaluated subtotal | 4029 |" in text
    assert "365 fired candidate states and 338 exact boundary fires" in text
    assert "| family | tasks | nonfull | candidates | actions | fires | overlap | valid | aligned |" in text
    assert "| task | family | reward | band | actions |" in text
    assert "candidate id | action id | task | seq | arm | status | emitted fire" in text
    assert "candidate id | action id | seq | service | endpoint | target" in text
    assert "repair/recovered-verifiers/email-ambiguous-cleanup/replay.json" in text
    assert "no document content was read before the external grant" in text
    assert "artifacts/env0/terminal/mock-gdrive/diff.json" in text
    assert text.count("| auth-app-install-scope-eval |") == 1
    assert text.count("| email-workflow-event-rsvp:354:identical_call_cap:1 |") == 1
    task_section = text.split("## Complete task table", 1)[1].split("## Complete fire table", 1)[0]
    fire_section = text.split("## Complete fire table", 1)[1].split(
        "## Complete non-pass candidate table", 1
    )[0]
    candidate_section = text.split("## Complete non-pass candidate table", 1)[1]
    assert sum(line.startswith("| ") for line in task_section.splitlines()) - 2 == 60
    assert sum(line.startswith("| ") for line in fire_section.splitlines()) - 2 == 338
    assert sum(line.startswith("| ") for line in candidate_section.splitlines()) - 2 == 391
    assert len(list((output / "figures").glob("*.png"))) >= 3

    before = {path.name: path.read_bytes() for path in output.rglob("*") if path.is_file()}
    second = make_figures.build(coverage, attribution, output)
    after = {path.name: path.read_bytes() for path in output.rglob("*") if path.is_file()}
    assert second == first
    assert after == before


def test_build_rejects_a_reviewed_reward_that_differs_from_coverage(tmp_path: Path) -> None:
    import make_figures

    coverage = json.loads((ANALYSIS_DIR / "coverage.json").read_text())
    attribution = json.loads((ANALYSIS_DIR / "attribution.json").read_text())
    changed = deepcopy(attribution)
    changed["tasks"][0]["reward"] = -99

    with pytest.raises(ValueError, match="reward"):
        make_figures.build(coverage, changed, tmp_path)


def test_alignment_requires_a_confirmed_same_reason_safety_fire(tmp_path: Path) -> None:
    import make_figures

    coverage = json.loads((ANALYSIS_DIR / "coverage.json").read_text())
    attribution = json.loads((ANALYSIS_DIR / "attribution.json").read_text())
    changed = deepcopy(attribution)
    group = next(
        group for group in changed["fire_groups"]
        if group["task"] == "email-workflow-event-rsvp"
    )
    group["relation"] = "same_reason"

    summary = make_figures.build(coverage, changed, tmp_path)

    assert summary["aligned_nonfull_count"] == 0


def test_build_rejects_duplicate_task_reviews(tmp_path: Path) -> None:
    import make_figures

    coverage = json.loads((ANALYSIS_DIR / "coverage.json").read_text())
    attribution = json.loads((ANALYSIS_DIR / "attribution.json").read_text())
    changed = deepcopy(attribution)
    changed["tasks"].append(deepcopy(changed["tasks"][0]))

    with pytest.raises(ValueError, match="exactly once"):
        make_figures.build(coverage, changed, tmp_path)
