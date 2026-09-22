import sys,json,subprocess
from pathlib import Path
R=Path('/workspace/.audit-worktrees/practicality-mainnet');sys.path.insert(0,str(R/'scripts/practicality'))
import run_public as P
C=P.C;w=C.w3();actors=C.actors(w);base=C.escrow_client(w,actors)
out=Path('/workspace/.local/base-mainnet-eval-20260916/results/mainnet/recovery.json')
m=json.loads(out.read_text());P.write_json(out.with_name('recovery-before-reconciliation.json'),m)
e=P.RecordedEscrow(w,base.contract.address,base.contract.abi,{a:P.LimitedSigner(k,base.contract.address) for a,k in actors.values() if k is not None});e.journal=P.EvidenceJournal(out,m)
h='0x9a7f28be015055d30529dd539bb487f838cbc5e4e841db5fa6a29e184da786df'
rcpt=w.eth.get_transaction_receipt(h);assert rcpt.status==1
tx=w.eth.get_transaction(h);assert tx['from']==actors['provider'][0] and tx['to']==base.contract.address
assert e.get_session(m['session_id'])[4]>0
m['attempts'][0].update(state='confirmed',transaction_hash=h,reconciled=True)
e._observe_receipt(rcpt);e.journal.transaction('commitTrace_reconciled',rcpt,None)
from aa_sdk import Accountability
store=P.HttpStore(C.STORE_URL);acc=Accountability('recovery',store=store,chain=e,provider_addr=actors['provider'][0])
m['summary']=acc.recover(m['session_id'],actors['challenger'][0])
assert len(m['transactions'])==1,'idempotent recovery sent another transaction'
cid=e.challenge(actors['challenger'][0],m['session_id'],1,P.BOND)
child=out.with_name('recovery-verifier.json');assert not child.exists()
subprocess.run([sys.executable,str(R/'scripts/practicality/run_public.py'),'verify','--output',str(child),'--challenge',str(cid),'--min-block',str(e._confirmed_block)],check=True)
v=json.loads(child.read_text());assert v['verdict']['violated'] is False
m['verdict']=v['verdict'];m['transactions']+=v['transactions'];m['attempts']+=v['attempts'];m['status']='passed_after_rpc_reconciliation';m['final_rpc_url']=C.RPC_URL
e.journal.write();print('Original session recovered and settled; no repeated tool action or final commitment')
