"""Analyze preserved receipts; never sign or send transactions."""
from pathlib import Path
from collections import defaultdict
import json
import statistics as st

R = Path(__file__).resolve().parents[1]
O = Path(__file__).resolve().parent

def summary(xs):
    return dict(n=len(xs), mean=st.mean(xs), min=min(xs), max=max(xs))

def analyze():
    x = json.loads((R/'mainnet04/evidence.json').read_text())
    assert x['status'] == 'passed', x['status']
    assert len(x['scenarios']) == 14 and len(x['checkpoint_runs']) == 9
    canonical_path=O/'canonical-receipts.json'
    canonical=json.loads(canonical_path.read_text()) if canonical_path.exists() else None
    final_fees={r['hash']:r['fee_wei'] for r in canonical['receipts']} if canonical and canonical['status']=='passed' else {}
    def fee(t):return final_fees.get(t['transaction_hash'].lower(),t['execution_plus_l1_fee_wei'])
    tx_args = {a['transaction_hash']: a for a in x['attempts'] if a['state'] == 'confirmed'}
    rows = []
    for row in x['checkpoint_runs']:
        records = sorted(row['records'], key=lambda r:r['seq'])
        n = len(records)
        births = [(r['seq'], r['ts']/1000) for r in records]
        anchors = []
        for t in row['transactions']:
            assert t['operation'] in ('checkpointTrace','commitTrace'), t['operation']
            count = tx_args[t['transaction_hash']]['function_args'][1] if t['operation']=='checkpointTrace' else n
            anchors.append((int(count), t['observed_at_unix']))
        delays = [min(t for count,t in anchors if count>=seq)-ts for seq,ts in births]
        assert min(delays)>=0
        # Evaluate immediately before every receipt to include the oldest exposed record.
        events = sorted([(ts,0,seq) for seq,ts in births]+[(ts,1,count) for count,ts in anchors])
        generated, anchored, max_count, max_age = 0,0,0,0.0
        for ts,kind,count in events:
            if kind == 0: generated=max(generated,count)
            pending=[birth for seq,birth in births if anchored<seq<=generated]
            max_count=max(max_count,len(pending))
            if pending:max_age=max(max_age,ts-min(pending))
            if kind == 1:anchored=max(anchored,count)
        fees=[fee(t) for t in row['transactions']]
        assert all(f is not None for f in fees)
        rows.append(dict(name=row['name'],strategy=row['strategy'],repetition=row['repetition'],
            records=n,checkpoint_count=len(row['checkpoints']),transactions=len(row['transactions']),
            total_fee_wei=sum(fees),mean_anchor_delay_seconds=st.mean(delays),
            max_unanchored_records=max_count,max_unanchored_age_seconds=max_age,
            action_span_seconds=row['action_span_seconds'],finalization_seconds=row['finalization_seconds']))
    groups={}
    for strategy in sorted({r['strategy'] for r in rows}):
        a=[r for r in rows if r['strategy']==strategy]
        groups[strategy]={k:summary([r[k] for r in a]) for k in rows[0] if k not in ('name','strategy','repetition')}
    ops=defaultdict(list)
    for t in x['transactions']:ops[t['operation']].append(t)
    op_summary={k:{'transactions':len(v), 'fee_wei':summary([fee(t) for t in v]),
        'receipt_observation_seconds':summary([t['confirmation_seconds'] for t in v])} for k,v in ops.items()}
    outcomes=[]
    for row in x['scenarios']:
        yes=row['verdict']['violated']
        assert row['reserve_before_wei']-row['reserve_after_wei']==(10**10 if yes else 0)
        assert row['challenger_balance_delta_wei']==(2*10**10 if yes else 0)
        assert row['provider_credit_delta_wei']==(0 if yes else 10**10)
        outcomes.append(dict(name=row['name'],violated=yes,reason=row['verdict']['reason']))
    result=dict(status='passed',source='mainnet04/evidence.json',source_revision=x['source_revision'],
        checkpoint_method='Per-record timestamps through client receipt observation; execution and L1 fees; setup excluded. Timings include 0.25s RPC pacing and 429 backoff. Three repetitions per policy, descriptive ranges.',
        checkpoint_rows=rows,checkpoint_summary=groups,operation_summary=op_summary,outcomes=outcomes,
        fee_source='canonical L2 receipts' if final_fees else 'provisional first-observed receipts',
        total_measured_transactions=len(x['transactions']),total_measured_fee_wei=sum(fee(t) for t in x['transactions']))
    (O/'public-summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))

if __name__=='__main__':analyze()
