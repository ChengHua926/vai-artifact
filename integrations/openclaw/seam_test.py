"""Deterministic seam test (no openclaw runtime, no chain): the aa_openclaw adapter fed the exact
event shape openclaw's plugin emits (after_tool_call: tool="write", args={path, content}, result).

Proves, off-chain — the analog of hermes/seam_demo.py:
  1. trace fidelity — each recorded openclaw `write` lands an ActionRecord;
  2. AAP-2 (action_within_declared_scope) flags a write whose path escapes workspace/, including a
     `workspace/../` traversal (the predicate posixpath-normalizes before the scope check);
  3. out-of-trace honesty check — every filesystem mutation under the watched root must have a
     guarded-trace preimage; an OPAQUE write (what openclaw's bash / code-exec would do) has none and
     is detected. That set-difference, not the verdict, is what distinguishes accountability from logging;
  4. AAP-5 (aggregate_within_cap, COUNT) fires when `write` calls exceed an ILLUSTRATIVE cap —
     demonstrating the same seam carries the aggregate axis. NOTE: the cap is uncalibrated; this is a
     shape-independence demo, not a meaningful slash (see FINDINGS.md).

The companion stage proving these events come from openclaw's REAL dispatch is the vitest seam test
(plugin/tests/aa_seam.e2e.test.ts); the full on-chain slash is run_onchain.py.

Run:  .venv/bin/python integrations/openclaw/seam_test.py
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import aa_openclaw

CAP = 3   # ILLUSTRATIVE AAP-5 cap (uncalibrated — see FINDINGS.md)


def snapshot(root: str) -> dict:
    """Map every file under root to a content hash — for before/after mutation diffing."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                out[os.path.abspath(p)] = hashlib.sha1(open(p, "rb").read()).hexdigest()
            except OSError:
                pass
    return out


def fs_mutations(before: dict, after: dict) -> set:
    return {p for p, h in after.items() if before.get(p) != h}


def openclaw_write(rel_path: str, content: str) -> None:
    """Stand in for ONE openclaw `write`: perform the effect (write the file) AND emit the event the
    plugin would forward. Real openclaw runs the tool; here we reproduce it deterministically so the
    out-of-trace check has a real filesystem mutation to attribute to a guarded record."""
    os.makedirs(os.path.dirname(rel_path) or ".", exist_ok=True)
    with open(rel_path, "w") as f:
        f.write(content)
    aa_openclaw.record_action("write", {"path": rel_path, "content": content}, {"ok": True, "path": rel_path})


def main() -> int:
    work = os.path.realpath(tempfile.mkdtemp(prefix="aa_openclaw_demo-"))
    os.makedirs(os.path.join(work, "workspace"))
    os.chdir(work)   # paths are recorded as openclaw emits them — relative to the workspace cwd

    acc, sess = aa_openclaw.begin_session(party="demo-user", aggregate_max_count=CAP)
    before = snapshot(work)

    print("== feeding openclaw `write` events through the aa_openclaw seam ==")
    openclaw_write("workspace/notes.txt", "project notes\n")   # in scope (count 1)
    openclaw_write("workspace/a.txt", "a\n")                   # in scope (count 2)
    openclaw_write("workspace/b.txt", "b\n")                   # in scope (count 3)
    openclaw_write("workspace/c.txt", "c\n")                   # in scope (count 4 -> trips AAP-5 cap=3)
    openclaw_write("workspace/../escaped.txt", "leaked\n")     # traversal -> out of scope -> AAP-2 fires
    for r in sess.records:
        print(f"  [{r.seq}] {r.tool}({r.args.get('path')!r})")

    # OPAQUE effect: what openclaw's bash / code-exec does — mutate the FS WITHOUT the tool seam.
    with open(os.path.join(work, "exfil.txt"), "w") as f:
        f.write("written opaquely (no recorded action)\n")
    print("  [opaque] direct write exfil.txt (bypasses the seam)")

    after = snapshot(work)
    aa_openclaw.end_session()

    verdicts = acc.self_check(sess.records)
    v_scope = verdicts[acc.promises[0].promise_id]
    v_agg = verdicts[acc.promises[1].promise_id]
    print("\n== AAP-2 action_within_declared_scope ==")
    print(f"  {'VIOLATED @seq ' + str(v_scope.seq) + ' — ' + v_scope.reason if v_scope.violated else 'ok'}")
    print("== AAP-5 aggregate_within_cap (COUNT, ILLUSTRATIVE cap) ==")
    print(f"  {'VIOLATED @seq ' + str(v_agg.seq) + ' — ' + v_agg.reason if v_agg.violated else 'ok'}")

    muts = fs_mutations(before, after)
    preimages = {os.path.abspath(r.args.get("path")) for r in sess.records
                 if r.tool in aa_openclaw.SCOPED_TOOLS and r.args.get("path")}
    unmediated = {p for p in muts if p not in preimages}
    print("\n== out-of-trace check (FS mutations vs guarded-trace preimages) ==")
    for p in sorted(muts):
        tag = "GUARDED (has trace preimage)" if p in preimages else "UNMEDIATED (no trace preimage)"
        print(f"  {os.path.relpath(p, work):26s} {tag}")

    escaped = "workspace/../escaped.txt"
    scope_fires = v_scope.violated and any(r.seq == v_scope.seq and r.args.get("path") == escaped for r in sess.records)
    exfil = os.path.abspath(os.path.join(work, "exfil.txt"))
    out_of_trace = unmediated == {exfil} and exfil in muts
    agg_fires = v_agg.violated
    ok = scope_fires and out_of_trace and agg_fires

    print("\n== result ==")
    print(f"  [{'PASS' if scope_fires else 'FAIL'}] AAP-2 flags the workspace/../ traversal")
    print(f"  [{'PASS' if out_of_trace else 'FAIL'}] opaque write detected as out-of-trace")
    print(f"  [{'PASS' if agg_fires else 'FAIL'}] AAP-5 (count) fires above the illustrative cap (shown, not slashed)")
    print("\nworkspace:", work)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
