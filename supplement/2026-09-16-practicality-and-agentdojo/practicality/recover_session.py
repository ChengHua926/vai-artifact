"""Recover the preserved session interrupted by a read-only RPC429, without replaying tools."""
import sys,json,time,subprocess
from pathlib import Path
R=Path('/workspace/.audit-worktrees/practicality-mainnet')
sys.path.insert(0,str(R/'scripts/practicality'))
import run_public as P
C=P.C
out=Path('/workspace/.local/base-mainnet-eval-20260916/results/mainnet/recovery.json')
assert not out.exists()
w=C.w3(); actors=C.actors(w); old=json.loads(out.with_name('evidence.json').read_text())
sid=next(a['function_args'][0] for a in old['attempts'] if a['operation']=='openSession')
base=C.escrow_client(w,actors)
accounts={a:P.LimitedSigner(k,base.contract.address) for a,k in actors.values() if k is not None}
e=P.RecordedEscrow(w,base.contract.address,base.contract.abi,accounts)
m={'status':'running','source_revision':old['source_revision'],'contract_address':base.contract.address,
   'cause':'official public RPC429 on checkpointCount read before final commitment',
   'recovery':'SDK Accountability.recover reads preserved store records; native tool not replayed',
   'rpc_url':C.RPC_URL,'session_id':sid,'transactions':[],'attempts':[]}
e.journal=P.EvidenceJournal(out,m)
from aa_sdk import Accountability
store=P.HttpStore(C.STORE_URL)
acc=Accountability('recovery',store=store,chain=e,provider_addr=actors['provider'][0])
assert e.get_session(sid)[4]==0
m['records']=store.get_records(sid)
m['before_checkpoints']=e.get_checkpoints(sid)
m['summary']=acc.recover(sid,actors['challenger'][0]);e.journal.write()
cid=e.challenge(actors['challenger'][0],sid,1,P.BOND)
child=out.with_name('recovery-verifier.json')
subprocess.run([sys.executable,str(R/'scripts/practicality/run_public.py'),'verify','--output',str(child),'--challenge',str(cid),'--min-block',str(e._confirmed_block)],check=True)
v=json.loads(child.read_text());assert v['verdict']['violated'] is False
m['verdict']=v['verdict'];m['transactions']+=v['transactions'];m['attempts']+=v['attempts'];m['status']='passed'
e.journal.write()
print('Recovered original session, same records, satisfied verdict',flush=True)
