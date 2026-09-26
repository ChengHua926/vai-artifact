"""Convert a tau2-bench SimulationRun (dict) into an aa_commons ActionRecord trace.

tau2 records each agent tool call in an assistant message's ``tool_calls``
(``{id, name, arguments, requestor}``) and each tool RESULT in a ``role: "tool"``
message whose ``id`` equals the originating tool_call id. We map that to the
ActionRecord stream the predicates read (tool name, args, and the result the agent saw).
"""
from __future__ import annotations

from aa_commons import ActionRecord


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "" if content is None else str(content)


def tau2_turns(sim: dict) -> list[dict]:
    """The conversation turns, aligned with the trace: each user/assistant message in order,
    with the seq numbers of the tool calls that message carried. seq counting mirrors
    tau2_to_trace exactly (assistant messages only), so `calls: [3]` in a turn means trace
    row seq 3 was issued inside that message. Lets a promise ask "what did the user say
    before call seq N" without re-parsing the raw benchmark files."""
    out, seq = [], 0
    for m in sim.get("messages", []):
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        calls = []
        if role == "assistant":
            for _ in (m.get("tool_calls") or []):
                seq += 1
                calls.append(seq)
        out.append({"role": role, "text": _text(m.get("content"))[:2000], "calls": calls})
    return out


def tau2_to_trace(sim: dict, session_id: str = "s") -> list[ActionRecord]:
    results = {}
    for m in sim.get("messages", []):
        if m.get("role") == "tool":
            results[m.get("id")] = _text(m.get("content"))
    recs, seq = [], 0
    for m in sim.get("messages", []):
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            seq += 1
            recs.append(ActionRecord(
                seq=seq, session_id=session_id,
                tool=tc.get("name"),
                args=dict(tc.get("arguments") or {}),
                result=results.get(tc.get("id")),
                ts=seq,
            ))
    return recs
