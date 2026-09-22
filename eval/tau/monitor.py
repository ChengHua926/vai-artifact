"""Run the existing Tau adapter/predicate monitor without changing its semantics."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

from eval.artifacts import source_file_record


REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_PATHS = (
    REPO_ROOT / "eval" / "predicates.py",
    REPO_ROOT / "eval" / "tau" / "adapter.py",
    REPO_ROOT / "eval" / "tau" / "promises.py",
)


def _load_exact_source(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load compiled Tau monitor source: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_COMPILED_TAU_PROMISES = _load_exact_source(
    "_compiled_tau_monitor_promises", _SOURCE_PATHS[2]
)


def _require_exact_source(module: Any, path: Path) -> None:
    module_path = getattr(module, "__file__", None)
    if module_path is None or Path(module_path).resolve(strict=True) != path.resolve(
        strict=True
    ):
        raise RuntimeError(f"compiled Tau monitor imported the wrong source for {path}")


def _modules():
    missing = object()
    previous_promises = sys.modules.get("promises", missing)
    previous_path = list(sys.path)
    try:
        adapter = importlib.import_module("eval.tau.adapter")
    finally:
        sys.path[:] = previous_path
        if previous_promises is missing:
            sys.modules.pop("promises", None)
        else:
            sys.modules["promises"] = previous_promises
    predicates = importlib.import_module("eval.predicates")
    _require_exact_source(adapter, _SOURCE_PATHS[1])
    _require_exact_source(predicates, _SOURCE_PATHS[0])
    adapter.promises = _COMPILED_TAU_PROMISES
    return adapter, predicates


def compiled_monitor_manifest() -> dict[str, Any]:
    """Identify and hash the exact compiled monitor whose fires are preserved."""

    return {
        "schema_version": 1,
        "implementation": "compiled_tau_monitor",
        "sources": [source_file_record(path, root=REPO_ROOT) for path in _SOURCE_PATHS],
    }


def _supplied_checks(trace: list[dict[str, Any]], inputs: dict[str, Any]) -> dict[str, list[str]]:
    supplied: dict[int, set[str]] = {}
    rejected = set(inputs.get("rejected") or ())
    for seq, checks in (inputs.get("scope_checks") or {}).items():
        supplied.setdefault(int(seq), set()).update(str(check.get("arm")) for check in checks)
    for seq, checks in (inputs.get("mandate_checks") or {}).items():
        supplied.setdefault(int(seq), set()).update(str(check.get("arm")) for check in checks)
    for cap in inputs.get("count_caps") or []:
        tools = set(cap.get("tools") or ())
        mode = cap.get("mode", "effects")
        for call in trace:
            seq = call.get("seq")
            if call.get("tool") in tools and (seq not in rejected or mode == "attempts"):
                supplied.setdefault(int(seq), set()).add(str(cap.get("arm")))
    return {str(seq): sorted(arms) for seq, arms in sorted(supplied.items())}


def run_compiled_monitor(
    domain: str,
    trace: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run the old adapter and shared predicate engine on an already-normalized trace."""

    if domain not in {"airline", "retail"}:
        raise ValueError(f"unsupported Tau domain: {domain}")
    adapter, predicates = _modules()
    execution = {"trace": trace, "turns": turns}
    inputs = adapter.build_inputs(execution, domain)
    fires = predicates.run(trace, inputs)
    return {
        "implementation": "compiled_tau_monitor",
        "fires": fires,
        "supplied_checks_by_seq": _supplied_checks(trace, inputs),
        "rejected_seqs": sorted(inputs.get("rejected") or ()),
    }
