"""Start the local accountability demo: deploy the escrow on anvil, post a bond, register the
provider's two promises (AAP-1 consent, AAP-2 scope), and drop into an interactive agent REPL.

Talk to the file agent normally. It honors consent the way real agents do: set a scoped standing
grant with /grant and matching deletes auto-proceed; anything else prompts you for a real yes/no
(logged as user_consent). Flip !rogue to simulate a misbehaving build that skips consent — that's
the AAP-1 violation. Ask it to touch a file outside workspace/ for the AAP-2 violation. /end
commits the session on-chain so you can challenge it.

Prereqs (separate terminals, all local): `anvil`; the store
(`.venv/bin/uvicorn app:app --app-dir packages/store --port 8000`); the verifier
(`.venv/bin/python scripts/verifier_service.py`); and the provider listener that delivers claim
evidence (`.venv/bin/python scripts/provider_service.py`). See README.md. Needs
ANTHROPIC_API_KEY for the chat turns.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))

import anthropic

import _config as C
from aa_sdk import Accountability, HttpStore, StoreUnavailable
from aa_sdk.chain import EscrowClient
from demo_agent import (CONSENT_PARAMS, MODEL, SCOPE_PARAMS, SYSTEM, TOOL_SCHEMAS, TOOLS,
                        AgentState, act, grant_scope, revoke_scope, setup_sandbox)

PROMISES = {"consent": ("no_destructive_without_consent", CONSENT_PARAMS),
            "scope": ("action_within_declared_scope", SCOPE_PARAMS)}

HELP = """commands:
  <anything>            talk to the file agent (it asks you before any unconsented delete)
  /grant <prefix>       standing consent: auto-accept deletes under <prefix>  (e.g. /grant workspace/tmp/)
  /revoke <prefix>      revoke a standing grant
  !rogue                toggle a misbehaving build that SKIPS the consent step (-> AAP-1 violation)
  /end                  commit this session on-chain (then run scripts/challenge.py)
  /help                 show this
  /quit                 commit (if needed) and exit
to violate AAP-1: /grant workspace/tmp/ , then !rogue , then "delete workspace/reports/q3.pdf"
to violate AAP-2: ask it to "read outside.txt" (a file outside workspace/)"""


def consent_prompt(target: str) -> bool:
    ans = input(f"   [consent] the agent wants to delete {target!r} — allow? [y/N] ").strip().lower()
    return ans in ("y", "yes")


def llm_turn(client, sess, state, history, user_msg):
    history.append({"role": "user", "content": user_msg})
    for _ in range(6):
        resp = client.messages.create(model=MODEL, max_tokens=1024, thinking={"type": "disabled"},
                                      output_config={"effort": "low"}, system=SYSTEM,
                                      tools=TOOL_SCHEMAS, messages=history)
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                print("agent>", b.text.strip())
        history.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                out = act(sess, state, b.name, dict(b.input), ask=consent_prompt)
            except Exception as e:  # keep the trace + loop clean on a tool error
                out = f"error: {e}"
            print(f"   [tool] {b.name}({dict(b.input)}) -> {out}")
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(out)})
        history.append({"role": "user", "content": results})


def main() -> None:
    w3 = C.w3()
    assert w3.is_connected(), f"anvil not reachable at {C.RPC_URL} — run `anvil`"
    ws = C.actors(w3)
    deployer, provider, challenger, verifier = (ws["deployer"][0], ws["provider"][0],
                                                ws["challenger"][0], ws["verifier"][0])

    print(f"deploying escrow on {C.RPC_URL} (owner=verifier {verifier}) …")
    escrow = EscrowClient.deploy(w3, C.ARTIFACT, deployer=deployer, verifier=verifier, accounts=C.accounts_map(ws))
    dep = {"address": escrow.contract.address, "deploy_block": escrow.deploy_block,
           "provider_addr": provider, "challenger_addr": challenger, "verifier_addr": verifier, "promises": {}}

    acc = Accountability("demo-provider", store=HttpStore(C.STORE_URL), chain=escrow, provider_addr=provider)
    for key, (predicate, params) in PROMISES.items():
        # born funded: each promise carries a reserve of two payouts (slashable twice before lapse)
        dep["promises"][key] = acc.register_promise(predicate, params, C.PAYOUT, reserve_wei=2 * C.PAYOUT)
    C.save_deployment(dep)

    setup_sandbox()
    print(f"\nescrow {escrow.contract.address} ; each promise reserved with {2 * C.PAYOUT} wei")
    for key, (predicate, params) in PROMISES.items():
        print(f"  promise[{key}] = id {dep['promises'][key]}  {predicate}  params={params}")
    print("\n" + HELP + "\n")

    client = anthropic.Anthropic()
    history: list = []
    sess = acc.session(party=challenger)
    state = AgentState(party=challenger)

    def commit():
        try:
            summary = sess.end()      # commits the hash of the records the store holds
        except StoreUnavailable as e:
            print(f"   [store] {e}\n   -> nothing committed; bring the store back and /end again")
            return None
        d = C.load_deployment(); d["session_id"] = summary["session_id"]; C.save_deployment(d)
        print(f"   committed session {summary['session_id'][:16]}… ({summary['n_actions']} actions) on-chain")
        return summary

    while True:
        try:
            msg = input("you> ").strip()
        except EOFError:
            break
        if not msg:
            continue
        if msg in ("/quit", "/exit"):
            break
        elif msg == "/help":
            print(HELP)
        elif msg.startswith("/grant "):
            scope = msg.split(None, 1)[1].strip()
            grant_scope(sess, state, scope)
            print(f"   [grant] standing consent for deletes under {scope!r}")
        elif msg.startswith("/revoke "):
            scope = msg.split(None, 1)[1].strip()
            revoke_scope(sess, state, scope)
            print(f"   [revoke] {scope!r}")
        elif msg == "!rogue":
            state.rogue = not state.rogue
            print(f"   [rogue] consent step is now {'OFF (agent skips consent)' if state.rogue else 'ON'}")
        elif msg == "/end":
            if commit() is None:
                continue                            # keep the session open for the retry
            print("   -> run: .venv/bin/python scripts/challenge.py   (then scripts/observe.py)")
            sess = acc.session(party=challenger)   # fresh session for further play
            state = AgentState(party=challenger)
        else:
            llm_turn(client, sess, state, history, msg)

    if sess.records:   # commit any uncommitted work on exit
        commit()


if __name__ == "__main__":
    main()
