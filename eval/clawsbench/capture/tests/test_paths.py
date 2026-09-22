from __future__ import annotations

from pathlib import Path

import pytest

from eval.clawsbench.capture.paths import resolve_research_root


def _dependencies(root: Path) -> Path:
    (root / "benchflow").mkdir(parents=True)
    (root / "env0").mkdir()
    return root.resolve()


def test_resolves_validated_siblings_from_canonical_and_worktree_checkouts(
    tmp_path: Path,
) -> None:
    research = _dependencies(tmp_path / "research")
    canonical = research / "agent_accountability_prototype"
    worktree = research / ".audit-worktrees/eval-consolidation"
    canonical.mkdir()
    worktree.mkdir(parents=True)

    assert resolve_research_root(canonical, environ={}) == research
    assert resolve_research_root(worktree, environ={}) == research


def test_explicit_research_root_is_validated(tmp_path: Path) -> None:
    configured = _dependencies(tmp_path / "dependencies")
    checkout = tmp_path / "elsewhere/prototype"
    checkout.mkdir(parents=True)

    assert resolve_research_root(
        checkout,
        environ={"CLAWSBENCH_RESEARCH_ROOT": str(configured)},
    ) == configured

    with pytest.raises(RuntimeError, match="must contain benchflow and env0"):
        resolve_research_root(
            checkout,
            environ={"CLAWSBENCH_RESEARCH_ROOT": str(tmp_path / "missing")},
        )
