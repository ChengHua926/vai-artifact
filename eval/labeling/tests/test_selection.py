"""Selection derivation tests: determinism, allocation, anonymization."""

import hashlib
import json
import re

import pytest

from eval.labeling import select_tasks


@pytest.fixture(scope="module")
def selection():
    return select_tasks.build_selection()


def test_same_seed_determinism(selection):
    assert select_tasks.build_selection() == selection


def test_allocation_matches_largest_remainder(selection):
    counts = {}
    for row in select_tasks._tau_rows():
        key = f"{row['model_id']}/{row['domain']}/{'pass' if row['native_pass'] else 'fail'}"
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    quotas = {key: count * 30 / total for key, count in counts.items()}
    expected = {key: int(quota) for key, quota in quotas.items()}
    order = sorted(
        counts, key=lambda key: (quotas[key] - expected[key], key), reverse=True
    )
    for key in order[: 30 - sum(expected.values())]:
        expected[key] += 1
    allocation = selection["tau"]["strata_allocation"]
    assert allocation == expected
    assert sum(allocation.values()) == 30


def test_runs_match_allocation(selection):
    counts = {}
    for row in selection["tau"]["runs"]:
        key = f"{row['model_id']}/{row['domain']}/{'pass' if row['native_pass'] else 'fail'}"
        counts[key] = counts.get(key, 0) + 1
    assert counts == selection["tau"]["strata_allocation"]


def test_check_passes_against_tracked_file(capsys):
    assert select_tasks.main(["--check"]) == 0
    assert "OK" in capsys.readouterr().out


def test_display_ids_unique_and_anonymous():
    tracked = json.loads(select_tasks.SELECTION_PATH.read_text())
    runs = tracked["tau"]["runs"]
    display_ids = [row["display_id"] for row in runs]
    assert sorted(display_ids) == [f"run-{i:02d}" for i in range(1, 31)]
    assert len(set(display_ids)) == 30
    for row in runs:
        assert re.fullmatch(r"run-\d{2}", row["display_id"])
        for hint in (row["model_id"], row["domain"]):
            assert hint.lower() not in row["display_id"].lower()


@pytest.fixture(scope="module")
def extension():
    return select_tasks.build_extension_selection()


@pytest.fixture(scope="module")
def tracked_v1():
    return json.loads(select_tasks.SELECTION_PATH.read_text())


def test_extension_is_exact_complement(extension, tracked_v1):
    all_case_ids = {row["case_id"] for row in select_tasks._tau_rows()}
    assert len(all_case_ids) == 328
    ext_case_ids = {row["case_id"] for row in extension["tau"]["runs"]}
    v1_case_ids = {row["case_id"] for row in tracked_v1["tau"]["runs"]}
    assert len(extension["tau"]["runs"]) == 298
    assert len(ext_case_ids) == 298
    assert ext_case_ids.isdisjoint(v1_case_ids)
    assert ext_case_ids | v1_case_ids == all_case_ids

    index = json.loads(select_tasks.CLAWS_INDEX.read_text())
    all_task_ids = {row["task"] for row in index["tasks"]}
    assert len(all_task_ids) == 60
    ext_tasks = set(extension["clawsbench"]["tasks"])
    v1_tasks = set(tracked_v1["clawsbench"]["tasks"])
    assert len(extension["clawsbench"]["tasks"]) == 30
    assert len(v1_tasks) == 30
    assert ext_tasks.isdisjoint(v1_tasks)
    assert ext_tasks | v1_tasks == all_task_ids

    assert extension["tau"]["count"] == 298
    assert extension["clawsbench"]["count"] == 30


def test_extension_display_ids(extension, tracked_v1):
    display_ids = [row["display_id"] for row in extension["tau"]["runs"]]
    assert display_ids == [f"ext-{i:03d}" for i in range(1, 299)]
    assert len(set(display_ids)) == 298
    v1_keys = set(tracked_v1["tau"]["runs"][0].keys())
    for row in extension["tau"]["runs"]:
        assert set(row.keys()) == v1_keys
        assert re.fullmatch(r"ext-\d{3}", row["display_id"])


def test_extension_is_deterministic(extension):
    assert select_tasks.build_extension_selection() == extension
    case_ids = [row["case_id"] for row in extension["tau"]["runs"]]
    assert case_ids != sorted(case_ids)


def test_extension_derived_from_records_v1_hash(extension):
    expected = hashlib.sha256(select_tasks.SELECTION_PATH.read_bytes()).hexdigest()
    assert extension["derived_from"]["sha256"] == expected
    assert extension["derived_from"]["file"] == "eval/labeling/selection_v1.json"
    assert extension["tau"]["seed"] == 20260909


def test_extension_write_and_check_roundtrip(tmp_path, monkeypatch, capsys):
    ext_dir = tmp_path / "extension"
    ext_path = ext_dir / "selection.json"
    monkeypatch.setattr(select_tasks, "EXT_DIR", ext_dir)
    monkeypatch.setattr(select_tasks, "EXT_SELECTION_PATH", ext_path)
    real_path = select_tasks.SELECTION_PATH.parent / "extension" / "selection.json"
    real_before = real_path.read_bytes() if real_path.exists() else None

    assert select_tasks.main(["--extension"]) == 0
    assert "wrote" in capsys.readouterr().out
    assert ext_path.exists()
    # The redirected write must never touch the tracked extension selection.
    assert (real_path.read_bytes() if real_path.exists() else None) == real_before

    assert select_tasks.main(["--extension", "--check"]) == 0
    assert "OK" in capsys.readouterr().out

    ext_path.write_text(ext_path.read_text().replace("ext-001", "ext-999", 1))
    assert select_tasks.main(["--extension", "--check"]) == 1
    assert "STALE" in capsys.readouterr().out

    assert select_tasks.main(["--check"]) == 0
    assert "OK" in capsys.readouterr().out


def test_calibration_selection_unchanged(selection):
    text = json.dumps(selection, indent=1, sort_keys=True) + "\n"
    assert text == select_tasks.SELECTION_PATH.read_text()
