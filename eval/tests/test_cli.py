from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval import dataset
from eval import run


def test_importing_supported_cli_does_not_import_benchmark_runtimes() -> None:
    command = (
        "import sys; import eval.run; "
        "print(any(name.startswith(('eval.agentdojo', "
        "'eval.tau')) for name in sys.modules))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == "False\n"


def test_verify_and_every_evaluate_selection_are_behaviorally_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    network: list[str] = []
    monkeypatch.setattr(
        dataset.urllib.request,
        "urlopen",
        lambda url: network.append(str(url)),
    )
    verified = SimpleNamespace(
        cohort=SimpleNamespace(cohort_id="paper_main_v1", accepted_shards=tuple(range(14)))
    )
    monkeypatch.setattr(run, "verify_paper_main_v1", lambda: verified)
    monkeypatch.setattr(run, "generate_evaluation_bundle", lambda *_args, **_kwargs: {"mode": "all"})
    monkeypatch.setattr(
        run,
        "generate_benchmark_bundle",
        lambda _output, *, benchmark, tau_root=None: {"mode": benchmark},
    )

    assert run.main(["verify"]) == 0
    assert capsys.readouterr().out == "PASS: paper_main_v1 (14 accepted shards)\n"

    expected_hashes = {
        "all": "2885692ece9ea50d7158a91914d0e89d4465c0e5af00a1c98d893a02d86bdbd3",
        "agentdojo": "d34b96eb07657884521ff48d274c51c03f600113ddcf7644c88dfb4ac47b6683",
        "tau": "4ba71cf7eaa815285dc9e83fce9d019f758acc4ea7703b6b66146b1e27274349",
    }
    for benchmark, expected_hash in expected_hashes.items():
        assert run.main(
            ["evaluate", "--benchmark", benchmark, "--output", str(tmp_path / benchmark)]
        ) == 0
        assert capsys.readouterr().out == f"{expected_hash}\n"

    assert network == []


def test_evaluate_requires_explicit_benchmark() -> None:
    with pytest.raises(SystemExit) as error:
        run.main(["evaluate"])

    assert error.value.code == 2


def test_evaluate_default_writes_to_consolidated_paper_main_root(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Path, Path | None]] = []

    def fake_generate(output: Path, *, tau_root: Path | None = None):
        calls.append((output, tau_root))
        return {"mode": "all"}

    monkeypatch.setattr(run, "generate_evaluation_bundle", fake_generate)

    assert run.main(["evaluate", "--benchmark", "all"]) == 0

    expected = Path(__file__).resolve().parents[1] / "paper_main_v1"
    assert calls == [(expected, None)]
    assert capsys.readouterr().out == (
        "2885692ece9ea50d7158a91914d0e89d4465c0e5af00a1c98d893a02d86bdbd3\n"
    )


def test_fetch_cli_forwards_unconfigured_local_archive_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "paper-main.tar"
    archive.write_bytes(b"archive")
    calls: list[tuple[Path | None, str | None, int | None]] = []
    restored = SimpleNamespace(
        cohort=SimpleNamespace(cohort_id="paper_main_v1", accepted_shards=tuple(range(14)))
    )

    def fake_fetch(
        archive_path: Path | None = None,
        *,
        archive_sha256: str | None = None,
        archive_bytes: int | None = None,
    ):
        calls.append((archive_path, archive_sha256, archive_bytes))
        return restored

    monkeypatch.setattr(run, "fetch_paper_main_v1", fake_fetch)

    assert run.main(
        [
            "fetch",
            "--archive",
            str(archive),
            "--sha256",
            "a" * 64,
            "--bytes",
            "7",
        ]
    ) == 0

    assert calls == [(archive, "a" * 64, 7)]
    assert capsys.readouterr().out == "RESTORED: paper_main_v1 (14 accepted shards)\n"
