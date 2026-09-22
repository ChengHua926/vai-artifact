from __future__ import annotations

from pathlib import Path

import pytest

from eval import dataset
from eval.artifacts import (
    canonical_case_key,
    canonical_json_bytes,
    canonical_jsonl_bytes,
    sha256_file,
    source_file_record,
    stable_case_id,
    validate_unique_case_rows,
    write_artifact_bundle,
)
from eval.dataset import Cohort, load_paper_main_v1


def test_canonical_json_and_jsonl_have_stable_exact_bytes() -> None:
    assert canonical_json_bytes({"z": [3, 2], "a": "é"}) == (
        b'{"a":"\xc3\xa9","z":[3,2]}'
    )

    rows = [
        {"case_key": "b", "value": 2},
        {"case_key": "a", "value": 1},
    ]
    assert canonical_jsonl_bytes(rows) == (
        b'{"case_key":"a","value":1}\n'
        b'{"case_key":"b","value":2}\n'
    )
    assert canonical_jsonl_bytes([]) == b""


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})


@pytest.mark.parametrize(
    "value",
    [
        {1: "integer key"},
        {"nested": {1: "integer key"}},
        {"nested": [{"valid": {False: "boolean key"}}]},
    ],
)
def test_canonical_json_rejects_non_string_object_keys_recursively(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="JSON object keys must be strings"):
        canonical_json_bytes(value)


def test_case_key_rejects_non_string_identity_keys_recursively() -> None:
    with pytest.raises(ValueError, match="JSON object keys must be strings"):
        canonical_case_key(
            benchmark="tau",
            model_id="glm47",
            identity={"nested": {1: "collision"}},
        )


def test_case_id_is_bound_to_canonical_key_and_current_outer_hash() -> None:
    key = canonical_case_key(
        benchmark="tau",
        model_id="glm47",
        identity={"trial": 0, "task_id": "7", "domain": "airline"},
    )
    assert key == (
        '{"benchmark":"tau","identity":{"domain":"airline",'
        '"task_id":"7","trial":0},"model_id":"glm47"}'
    )
    assert stable_case_id("a" * 64, key) == (
        "case_2ab70fca060888a19a66965e9b548adddd6248d49e3aa15823834add33fe5f59"
    )
    assert stable_case_id("b" * 64, key) != stable_case_id("a" * 64, key)
    assert stable_case_id("a" * 64, key + "x") != stable_case_id("a" * 64, key)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {"case_id": "case_1", "case_key": "key_a"},
                {"case_id": "case_2", "case_key": "key_a"},
            ],
            "duplicate case_key",
        ),
        (
            [
                {"case_id": "case_1", "case_key": "key_a"},
                {"case_id": "case_1", "case_key": "key_b"},
            ],
            "duplicate case_id",
        ),
    ],
)
def test_case_row_validation_rejects_duplicate_keys_or_ids(
    rows: list[dict[str, str]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_unique_case_rows(rows)


def test_file_hash_and_source_record_use_relative_posix_paths(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source = source_root / "nested" / "trace.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"abc")

    expected_hash = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_file(source) == expected_hash
    assert source_file_record(source, root=source_root) == {
        "path": "nested/trace.json",
        "sha256": expected_hash,
    }

    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="outside source root"):
        source_file_record(outside, root=source_root)


def test_artifact_bundle_is_exact_and_independent_of_input_order(tmp_path: Path) -> None:
    output = tmp_path / "results"
    summary = {"model_id": "glm47", "counts": {"ok": 2}}
    rows = [
        {"case_id": "case_b", "case_key": "b", "value": 2},
        {"case_id": "case_a", "case_key": "a", "value": 1},
    ]

    hashes = write_artifact_bundle(
        output,
        row_files={"runs.jsonl": rows},
        summary=summary,
    )

    expected_rows = (
        b'{"case_id":"case_a","case_key":"a","value":1}\n'
        b'{"case_id":"case_b","case_key":"b","value":2}\n'
    )
    expected_summary = b'{"counts":{"ok":2},"model_id":"glm47"}'
    assert (output / "runs.jsonl").read_bytes() == expected_rows
    assert (output / "summary.json").read_bytes() == expected_summary
    assert hashes == {
        "runs.jsonl": "530effce694fc95a7ed86e2bb66a6b23516ec280a61efe2b20baa5d04aad5cdc",
        "summary.json": "88b59c1d5588540b4ef73375cd802ebbf31e96621ceaa1a360e5b4d1fbba3bbb",
    }
    assert (output / "SHA256SUMS").read_text() == (
        "530effce694fc95a7ed86e2bb66a6b23516ec280a61efe2b20baa5d04aad5cdc  runs.jsonl\n"
        "88b59c1d5588540b4ef73375cd802ebbf31e96621ceaa1a360e5b4d1fbba3bbb  summary.json\n"
    )

    first_bytes = {
        path.name: path.read_bytes() for path in output.iterdir() if path.is_file()
    }
    write_artifact_bundle(
        output,
        row_files={"runs.jsonl": list(reversed(rows))},
        summary={"counts": {"ok": 2}, "model_id": "glm47"},
    )
    assert {
        path.name: path.read_bytes() for path in output.iterdir() if path.is_file()
    } == first_bytes


def test_artifact_bundle_rejects_unsafe_or_unlisted_output_files(tmp_path: Path) -> None:
    output = tmp_path / "results"
    with pytest.raises(ValueError, match="safe relative"):
        write_artifact_bundle(
            output,
            row_files={"../runs.jsonl": []},
            summary={},
        )

    output.mkdir()
    (output / "stale.json").write_text("{}")
    with pytest.raises(ValueError, match="unexpected output artifact"):
        write_artifact_bundle(output, row_files={"runs.jsonl": []}, summary={})


@pytest.mark.parametrize(
    "artifact_path",
    [
        "bad\nparent/runs.jsonl",
        "bad\rname.jsonl",
        "bad\tname.jsonl",
        "bad\x7fname.jsonl",
    ],
)
def test_artifact_bundle_rejects_control_characters_in_any_path_component(
    tmp_path: Path, artifact_path: str
) -> None:
    with pytest.raises(ValueError, match="control characters"):
        write_artifact_bundle(
            tmp_path / "results",
            row_files={artifact_path: []},
            summary={},
        )


def test_artifact_bundle_rejects_symlinked_output_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked output ancestor"):
        write_artifact_bundle(
            linked_parent / "results",
            row_files={"runs.jsonl": []},
            summary={},
        )
    assert not (real_parent / "results").exists()


def test_paper_main_loader_delegates_to_verified_cohort_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "cohort.lock.json"
    lock.write_text("{}")
    outer = tmp_path / "capture_manifest.json"
    outer.write_bytes(b"sealed outer")
    fake = Cohort(
        cohort_id="paper_main_v1",
        lock_path=lock,
        outer_manifest_path=outer,
        accepted_shards=(),
    )
    calls: list[Path] = []

    def fake_load_cohort(lock_path: Path) -> Cohort:
        calls.append(lock_path)
        return fake

    monkeypatch.setattr(dataset, "load_cohort", fake_load_cohort)
    loaded = load_paper_main_v1(lock)

    assert calls == [lock]
    assert loaded.cohort is fake
    assert loaded.outer_sha256 == (
        "0b0675ac155711014568f95ed63e35829c3d0573475a3ce2dd6c8de3c39bd62e"
    )
