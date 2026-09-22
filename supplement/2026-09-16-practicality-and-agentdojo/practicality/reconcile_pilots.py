"""Close interrupted empty session and retire pilot promises before fresh measurement."""
import sys,json
from pathlib import Path
R=Path('/workspace/.audit-worktrees/practicality-mainnet');sys.path.insert(0,str(R/'scripts/practicality'))
import run_public as P
from aa_sdk import Accountability, Session
from aa_commons import params_hash,registry
C=P.C;w=C.w3();actors=C.actors(w);base=C.escrow_client(w,actors)
out=Path('/workspace/.local/base-mainnet-eval-20260916/results/pilot-cleanup.json');assert not out.exists()
m={'status':'running','contract':base.contract.address,'transactions':[],'attempts':[],'reason':'close empty pilot and retire old promises so their path scopes cannot affect new sessions'}
e=P.RecordedEscrow(w,base.contract.address,base.contract.abi,{a:P.LimitedSigner(k,base.contract.address) for a,k in actors.values() if k is not None});e.journal=P.EvidenceJournal(out,m)
for signer in e.accounts.values():signer.journal=e.journal
oldpath=out.parent/'mainnet03/evidence.json';old=json.loads(oldpath.read_text())
signed=old['signed_transactions'][-1];rcpt=w.eth.get_transaction_receipt(signed['hash']);assert rcpt.status==1
P.write_json(out.parent/'mainnet03/reconciled-open-receipt.json',json.loads(w.to_json(rcpt)))
old['attempts'][-1].update(state='confirmed',transaction_hash=signed['hash'],reconciled=True)
old['status']='interrupted_before_tool_execution';P.write_json(oldpath,old)
e._observe_receipt(rcpt)
sid=old['attempts'][-1]['function_args'][0]
store=P.HttpStore(C.STORE_URL)
assert store.get_records(sid)==[]
assert e.get_session(sid)[0]==actors['provider'][0]
acc=Accountability('pilot-cleanup',store=store,chain=e,provider_addr=actors['provider'][0])
session=Session(acc=acc,session_id=sid,party=actors['challenger'][0])
m['empty_session_summary']=session.end()
for pid in [1,2,3]:
    promise=store.get_promise(pid);chain=e.get_promise(pid)
    assert P.Web3.to_hex(chain[2])==params_hash(promise['params'])
    assert P.Web3.to_hex(chain[1])==registry.predicate_hash_for(promise['predicate'])
    assert chain[6]==0
    e.retire_promise(actors['provider'][0],pid)
    assert e.get_promise(pid)[6]>0
m['status']='passed';e.journal.write();print('Pilot session finalized empty; three pilot promises retired')
