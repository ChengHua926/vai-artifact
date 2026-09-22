from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval import dataset


def _metadata(archive: object = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "cohort_id": "paper_main_v1",
        "corpus_directory": "corpus",
        "archive": archive,
    }


def _write_metadata(root: Path, archive: object = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "dataset.json").write_text(json.dumps(_metadata(archive)))


def _tar_bytes(*members: tuple[tarfile.TarInfo, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for member, content in members:
            member.mtime = 0
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content) if member.isreg() else None)
    return stream.getvalue()


def _regular(name: str, content: bytes = b"sealed evidence\n") -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), content


def test_build_archive_is_byte_identical_with_normalized_sorted_members(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "source-corpus"
    (corpus / "nested").mkdir(parents=True)
    (corpus / "b.txt").write_bytes(b"b\n")
    (corpus / "nested" / "a.txt").write_bytes(b"a\n")
    (corpus / "é.txt").write_bytes(b"unicode\n")
    os.chmod(corpus / "b.txt", 0o600)
    os.utime(corpus / "b.txt", (1_700_000_000, 1_700_000_000))
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_metadata = dataset.build_paper_main_archive(corpus, first)
    os.chmod(corpus / "b.txt", 0o777)
    os.utime(corpus / "b.txt", (1_800_000_000, 1_800_000_000))
    second_metadata = dataset.build_paper_main_archive(corpus, second)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes()[4:8] == b"\0\0\0\0"
    assert first_metadata == dataset.ArchiveMetadata(
        filename="first.tar.gz",
        sha256=hashlib.sha256(first.read_bytes()).hexdigest(),
        bytes=len(first.read_bytes()),
        url=None,
    )
    assert second_metadata.filename == "second.tar.gz"
    assert second_metadata.sha256 == first_metadata.sha256
    assert second_metadata.bytes == first_metadata.bytes
    with tarfile.open(first, mode="r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "corpus",
            "corpus/b.txt",
            "corpus/nested",
            "corpus/nested/a.txt",
            "corpus/é.txt",
        ]
        assert all(member.uid == member.gid == member.mtime == 0 for member in members)
        assert all(member.uname == member.gname == "" for member in members)
        assert all(
            member.mode == (0o755 if member.isdir() else 0o644)
            for member in members
        )
        assert all(member.isdir() or member.isreg() for member in members)
        extracted = archive.extractfile("corpus/nested/a.txt")
        assert extracted is not None
        assert extracted.read() == b"a\n"


def test_build_archive_rejects_symlink_without_writing_destination(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "source-corpus"
    corpus.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside\n")
    (corpus / "linked.txt").symlink_to(target)
    destination = tmp_path / "paper-main.tar.gz"

    with pytest.raises(ValueError, match="symlink"):
        dataset.build_paper_main_archive(corpus, destination)

    assert not destination.exists()


def test_build_archive_rejects_non_regular_file_without_opening_it(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "source-corpus"
    corpus.mkdir()
    fifo = corpus / "stream"
    os.mkfifo(fifo)
    destination = tmp_path / "paper-main.tar.gz"

    with pytest.raises(ValueError, match="non-regular"):
        dataset.build_paper_main_archive(corpus, destination)

    assert not destination.exists()


def test_build_archive_rejects_destination_inside_source_without_mutation(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "source-corpus"
    corpus.mkdir()
    evidence = corpus / "evidence.txt"
    evidence.write_text("sealed\n")
    destination = corpus / "nested" / "paper-main.tar.gz"

    with pytest.raises(ValueError, match="outside corpus source"):
        dataset.build_paper_main_archive(corpus, destination)

    assert evidence.read_text() == "sealed\n"
    assert not destination.parent.exists()


def test_build_archive_rejects_member_name_the_restore_path_would_reject(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "source-corpus"
    corpus.mkdir()
    (corpus / "bad\\name.txt").write_text("unsafe\n")
    destination = tmp_path / "paper-main.tar.gz"

    with pytest.raises(ValueError, match="safe relative path"):
        dataset.build_paper_main_archive(corpus, destination)

    assert not destination.exists()


def test_dataset_metadata_accepts_explicitly_unconfigured_archive(tmp_path: Path) -> None:
    root = tmp_path / "paper_main_v1"
    _write_metadata(root)

    metadata = dataset.load_dataset_metadata(root / "dataset.json")

    assert metadata.schema_version == 1
    assert metadata.cohort_id == "paper_main_v1"
    assert metadata.corpus_directory == "corpus"
    assert metadata.archive is None
    assert dataset.PAPER_MAIN_V1_ROOT.name == "paper_main_v1"
    assert dataset.PAPER_MAIN_V1_LOCK == dataset.PAPER_MAIN_V1_ROOT / "cohort.lock.json"


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"cohort_id": "other"}, "cohort_id"),
        ({"corpus_directory": "raw"}, "corpus_directory"),
        (
            {
                "archive": {
                    "filename": "paper-main.tar",
                    "sha256": "a" * 64,
                    "bytes": 1,
                    "url": "http://example.test/paper-main.tar",
                }
            },
            "HTTPS",
        ),
        (
            {
                "archive": {
                    "filename": "paper-main.tar",
                    "sha256": "a" * 64,
                    "bytes": 1,
                    "url": "https:///paper-main.tar",
                }
            },
            "HTTPS",
        ),
        (
            {
                "archive": {
                    "filename": "../paper-main.tar",
                    "sha256": "a" * 64,
                    "bytes": 1,
                    "url": None,
                }
            },
            "filename",
        ),
    ],
)
def test_dataset_metadata_fails_closed_on_schema_drift(
    tmp_path: Path, update: dict[str, object], message: str
) -> None:
    root = tmp_path / "paper_main_v1"
    value = _metadata()
    value.update(update)
    root.mkdir()
    (root / "dataset.json").write_text(json.dumps(value))

    with pytest.raises(ValueError, match=message):
        dataset.load_dataset_metadata(root / "dataset.json")


def test_unconfigured_fetch_requires_local_hash_and_size_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "paper_main_v1"
    _write_metadata(root)
    archive = tmp_path / "paper-main.tar"
    archive.write_bytes(_tar_bytes(_regular("corpus/evidence.txt")))
    network: list[str] = []
    monkeypatch.setattr(
        dataset.urllib.request,
        "urlopen",
        lambda url: network.append(str(url)),
    )

    with pytest.raises(ValueError, match="archive is not configured"):
        dataset.fetch_paper_main_v1(root=root)
    with pytest.raises(ValueError, match="--sha256 and --bytes"):
        dataset.fetch_paper_main_v1(archive, root=root)

    assert network == []
    assert not (root / "corpus").exists()


def test_local_fetch_verifies_extracts_atomically_and_runs_sealed_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "paper_main_v1"
    _write_metadata(root)
    archive = tmp_path / "paper-main.tar"
    content = _tar_bytes(
        _regular("corpus/accepted/a.json", b"a\n"),
        _regular("corpus/accepted/b.json", b"b\n"),
    )
    archive.write_bytes(content)
    verified = SimpleNamespace(cohort=SimpleNamespace(cohort_id="paper_main_v1"))
    calls: list[Path] = []

    def fake_verify(*, root: Path = dataset.PAPER_MAIN_V1_ROOT):
        calls.append(root)
        assert (root / "corpus" / "accepted" / "a.json").read_bytes() == b"a\n"
        return verified

    monkeypatch.setattr(dataset, "verify_paper_main_v1", fake_verify)

    result = dataset.fetch_paper_main_v1(
        archive,
        archive_sha256=hashlib.sha256(content).hexdigest(),
        archive_bytes=len(content),
        root=root,
    )

    assert result is verified
    assert calls == [root]
    assert (root / "corpus" / "accepted" / "b.json").read_bytes() == b"b\n"


@pytest.mark.parametrize(
    ("member", "message"),
    [
        (tarfile.TarInfo("outside.txt"), "top-level corpus"),
        (tarfile.TarInfo("../escape.txt"), "safe relative"),
        (tarfile.TarInfo("/corpus/escape.txt"), "safe relative"),
        (tarfile.TarInfo("corpus/link"), "regular files and directories"),
        (tarfile.TarInfo("corpus/fifo"), "regular files and directories"),
    ],
)
def test_fetch_rejects_unsafe_tar_members_before_restoring_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: tarfile.TarInfo,
    message: str,
) -> None:
    root = tmp_path / "paper_main_v1"
    _write_metadata(root)
    if member.name.endswith("link"):
        member.type = tarfile.SYMTYPE
        member.linkname = "accepted/a.json"
    elif member.name.endswith("fifo"):
        member.type = tarfile.FIFOTYPE
    content = _tar_bytes((member, b""))
    archive = tmp_path / "bad.tar"
    archive.write_bytes(content)
    monkeypatch.setattr(
        dataset,
        "load_paper_main_v1",
        lambda *_args, **_kwargs: pytest.fail("sealed loader must not run"),
    )

    with pytest.raises(ValueError, match=message):
        dataset.fetch_paper_main_v1(
            archive,
            archive_sha256=hashlib.sha256(content).hexdigest(),
            archive_bytes=len(content),
            root=root,
        )

    assert not (root / "corpus").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_fetch_rejects_nonempty_target_without_changing_it(tmp_path: Path) -> None:
    root = tmp_path / "paper_main_v1"
    _write_metadata(root)
    corpus = root / "corpus"
    corpus.mkdir()
    existing = corpus / "keep.txt"
    existing.write_text("keep")
    content = _tar_bytes(_regular("corpus/new.txt"))
    archive = tmp_path / "paper-main.tar"
    archive.write_bytes(content)

    with pytest.raises(ValueError, match="absent or empty"):
        dataset.fetch_paper_main_v1(
            archive,
            archive_sha256=hashlib.sha256(content).hexdigest(),
            archive_bytes=len(content),
            root=root,
        )

    assert existing.read_text() == "keep"
    assert not (corpus / "new.txt").exists()


def test_fetch_rejects_dangling_symlink_corpus_target(tmp_path: Path) -> None:
    root = tmp_path / "paper_main_v1"
    _write_metadata(root)
    corpus = root / "corpus"
    corpus.symlink_to(tmp_path / "missing", target_is_directory=True)
    content = _tar_bytes(_regular("corpus/new.txt"))
    archive = tmp_path / "paper-main.tar"
    archive.write_bytes(content)

    with pytest.raises(ValueError, match="absent or empty"):
        dataset.fetch_paper_main_v1(
            archive,
            archive_sha256=hashlib.sha256(content).hexdigest(),
            archive_bytes=len(content),
            root=root,
        )

    assert corpus.is_symlink()


def test_fetch_and_verify_reject_symlinked_dataset_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    _write_metadata(real_root)
    linked_root = tmp_path / "paper_main_v1"
    linked_root.symlink_to(real_root, target_is_directory=True)
    content = _tar_bytes(_regular("corpus/new.txt"))
    archive = tmp_path / "paper-main.tar"
    archive.write_bytes(content)

    with pytest.raises(ValueError, match="dataset root"):
        dataset.fetch_paper_main_v1(
            archive,
            archive_sha256=hashlib.sha256(content).hexdigest(),
            archive_bytes=len(content),
            root=linked_root,
        )
    with pytest.raises(ValueError, match="dataset root"):
        dataset.verify_paper_main_v1(root=linked_root)

    assert not (real_root / "corpus").exists()


def test_configured_fetch_is_the_only_path_that_opens_https_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "paper_main_v1"
    content = _tar_bytes(_regular("corpus/evidence.txt"))
    archive_metadata = {
        "filename": "paper-main.tar",
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "url": "https://example.test/paper-main.tar",
    }
    _write_metadata(root, archive_metadata)
    urls: list[str] = []

    def fake_urlopen(url: str):
        urls.append(str(url))
        return io.BytesIO(content)

    monkeypatch.setattr(dataset.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        dataset,
        "verify_paper_main_v1",
        lambda *, root=dataset.PAPER_MAIN_V1_ROOT: SimpleNamespace(
            cohort=SimpleNamespace(cohort_id="paper_main_v1")
        ),
    )

    dataset.fetch_paper_main_v1(root=root)

    assert urls == ["https://example.test/paper-main.tar"]
    assert (root / "corpus" / "evidence.txt").read_bytes() == b"sealed evidence\n"


def test_verify_checks_optional_committed_checksum_manifest_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "paper_main_v1"
    _write_metadata(root)
    evidence = root / "seal" / "evidence.json"
    evidence.parent.mkdir()
    evidence.write_bytes(b"{}\n")
    digest = "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
    (root / "SHA256SUMS").write_text(f"{digest}  seal/evidence.json\n")
    verified = SimpleNamespace(cohort=SimpleNamespace(cohort_id="paper_main_v1"))
    monkeypatch.setattr(dataset, "load_paper_main_v1", lambda lock_path: verified)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    assert dataset.verify_paper_main_v1(root=root) is verified
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before

    evidence.write_bytes(b"forged\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        dataset.verify_paper_main_v1(root=root)


def test_verify_rejects_dangling_sha256sums_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "paper_main_v1"
    _write_metadata(root)
    (root / "SHA256SUMS").symlink_to(root / "missing-checksums")
    verified = SimpleNamespace(cohort=SimpleNamespace(cohort_id="paper_main_v1"))
    monkeypatch.setattr(dataset, "load_paper_main_v1", lambda lock_path: verified)

    with pytest.raises(ValueError, match="SHA256SUMS must be a regular file"):
        dataset.verify_paper_main_v1(root=root)
