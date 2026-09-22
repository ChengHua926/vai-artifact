"""Replay existing v4 commitments without importing mutable benchmark policy code.

The checked-in source bundle is the exact text committed by benchmark_policy_v4.
Its seven eval modules execute in private module objects with a controlled import
map; none are inserted into sys.modules or loaded from promise parameters. This
is a loader for trusted repository code, not a sandbox for untrusted Python.

The bundle also retains the four original commons files for exact hash identity.
Runtime API objects (ActionRecord, Verdict, PredicateSpec, trace_hash, registry)
and the SDK class are shared with the installed packages. The frozen benchmark
evaluate function uses their stable data representation and registry interface,
not their current built-in predicates or parameter validation. Every benchmark
decision, reducer and helper remains bound to its frozen module globals.

Only old commitments should use this evaluator. It is registered by historical
hash and is never offered for a new promise in the current catalog.
"""
from __future__ import annotations

import builtins
from importlib.util import resolve_name
import json
from pathlib import Path
from threading import RLock
from types import ModuleType

from aa_commons import PredicateSpec, registry
from aa_commons.ids import predicate_hash


SOURCE_FILE = Path(__file__).with_name("baseline_v4_sources.json")
PREDICATE_HASH = "0x69b4c18eea4a77458460d504f5e4babc6c75c6e82d49c9f2be7cae89a6856c8c"
_ROOT = Path(__file__).resolve().parents[2]
_LOCK = RLock()
_RUNTIME: ModuleType | None = None
_SPEC: PredicateSpec | None = None


def _load_frozen(source_bundle: str) -> ModuleType:
    if predicate_hash(source_bundle) != PREDICATE_HASH:
        raise ValueError("historical v4 source bundle does not match its committed hash")
    sources = json.loads(source_bundle)
    paths = {path[:-3].replace("/", "."): path for path in sources if path.startswith("eval/")}
    modules: dict[str, ModuleType] = {}
    packages = {".".join(name.split(".")[:i])
                for name in paths for i in range(1, len(name.split(".")))}
    for name in sorted(packages, key=lambda value: value.count(".")):
        package = ModuleType(name)
        package.__package__ = name
        package.__path__ = []
        modules[name] = package
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(modules[parent], child, package)

    def load(name: str) -> ModuleType:
        if name in modules:
            return modules[name]
        if name not in paths:
            raise ImportError(f"{name} is not in the frozen v4 source bundle")
        path = paths[name]
        module = ModuleType(name)
        module.__package__ = name.rpartition(".")[0]
        module.__file__ = str(_ROOT / path)
        module.__builtins__ = {**vars(builtins), "__import__": frozen_import}
        modules[name] = module
        parent, child = name.rsplit(".", 1)
        setattr(modules[parent], child, module)
        exec(compile(sources[path], module.__file__, "exec"), module.__dict__)
        return module

    def frozen_import(name, globals=None, locals=None, fromlist=(), level=0):
        full_name = resolve_name("." * level + name, globals["__package__"]) if level else name
        if full_name == "eval" or full_name.startswith("eval."):
            module = load(full_name)
            if fromlist:
                for item in fromlist:
                    if item != "*" and not hasattr(module, item):
                        load(full_name + "." + item)
                return module
            return modules[full_name.split(".")[0]]
        return builtins.__import__(name, globals, locals, fromlist, level)

    frozen = load("eval.reference_v2.runtime")
    # Include even helpers not traversed by a given trace, so they can never
    # resolve through mutable eval modules later.
    for name in paths:
        load(name)
    frozen.source_bundle = lambda: source_bundle
    frozen.ensure_registered = ensure_registered
    return frozen


def runtime() -> ModuleType:
    """Return the cached frozen runtime (analyze_records/evaluate/capture helpers).

    Its old verify_case helper is not a new-registration API: v4 is historical
    only. Use ensure_registered().evaluate for old-hash verification.
    """
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is None:
            _RUNTIME = _load_frozen(SOURCE_FILE.read_text())
        return _RUNTIME


def ensure_registered() -> PredicateSpec:
    """Register the original implementation for committed-hash resolution only."""
    global _SPEC
    with _LOCK:
        if _SPEC is None:
            frozen = runtime()
            _SPEC = PredicateSpec(
                frozen.SPEC_ID, 4, frozen.evaluate,
                doc="Historical benchmark policy v4; existing commitments only",
                number="EVAL-1", source_bundle=frozen.source_bundle(),
                validate_params=frozen.validate)
        if registry.resolve_hash(PREDICATE_HASH) is not _SPEC:
            registry.register_historical(_SPEC)
        return _SPEC
