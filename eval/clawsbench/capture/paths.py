"""Checkout-independent dependency discovery for ClawsBench capture."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


_DEPENDENCIES = ("benchflow", "env0")


def _has_dependencies(root: Path) -> bool:
    return all((root / name).is_dir() for name in _DEPENDENCIES)


def resolve_research_root(
    prototype_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Find the directory containing the required sibling checkouts."""
    active_environ = os.environ if environ is None else environ
    explicit = active_environ.get("CLAWSBENCH_RESEARCH_ROOT")
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not _has_dependencies(candidate):
            raise RuntimeError(
                "CLAWSBENCH_RESEARCH_ROOT must contain benchflow and env0: "
                f"{candidate}"
            )
        return candidate

    checkout = Path(prototype_root).expanduser().resolve()
    for candidate in checkout.parents:
        if _has_dependencies(candidate):
            return candidate
    raise RuntimeError(
        "cannot locate sibling benchflow and env0 checkouts; "
        "set CLAWSBENCH_RESEARCH_ROOT"
    )
