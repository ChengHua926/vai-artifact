"""Print reproducible physical source-line and pinned-patch counts as JSON."""
from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def is_test(path):
    return (any(part in {"tests", "test"} for part in path.parts)
            or path.name.startswith("test_") or ".test." in path.name or ".spec." in path.name)


def source_counts(directory, extensions):
    files = {}
    for path in sorted((ROOT / directory).rglob("*")):
        if not path.is_file() or path.suffix not in extensions:
            continue
        relative = path.relative_to(ROOT)
        if any(part in {"tests", "test", "node_modules", "__pycache__", "patches", "dist"}
               for part in relative.parts):
            continue
        if is_test(path):
            continue
        files[str(relative)] = len(path.read_text().splitlines())
    return {"lines": sum(files.values()), "files": files}


def main():
    packages = {name: source_counts(f"packages/{name}", {".py"})
                for name in ("commons", "sdk", "store", "verifier")}
    adapters = {}
    for name, directories in {
        "hermes": ["integrations/hermes/aa_hermes"],
        "openclaw": ["integrations/openclaw/aa_openclaw", "integrations/openclaw/aa_helper",
                     "integrations/openclaw/plugin/src"],
    }.items():
        files = {}
        for directory in directories:
            files.update(source_counts(directory, {".py", ".ts"})["files"])
        patches = {}
        for patch in sorted((ROOT / "integrations" / name / "patches").glob("*.patch")):
            rows = subprocess.check_output(["git", "apply", "--numstat", str(patch)], text=True).splitlines()
            changes = [
                {"added": int(a), "removed": int(d), "file": p}
                for a, d, p in (row.split("\t", 2) for row in rows)
            ]
            patches[str(patch.relative_to(ROOT))] = {
                "implementation": [row for row in changes if not is_test(Path(row["file"]))],
                "tests": [row for row in changes if is_test(Path(row["file"]))],
            }
        adapters[name] = {"lines": sum(files.values()), "files": files, "patches": patches}
    print(json.dumps({
        "definition": "physical source lines including blank lines and comments; package and adapter totals exclude tests, dependencies and generated outputs; native patch implementation and test changes are listed separately",
        "python_packages": packages,
        "python_package_total": sum(p["lines"] for p in packages.values()),
        "solidity_contract_lines": len((ROOT / "contracts/src/Escrow.sol").read_text().splitlines()),
        "adapters": adapters,
    }, indent=2))


if __name__ == "__main__":
    main()
