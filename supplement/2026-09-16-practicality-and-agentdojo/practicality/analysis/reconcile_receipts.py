"""Read-only post-run receipt and wallet reconciliation. No private key access."""
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import json
import time
import sys
import requests

R=Path(__file__).resolve().parents[1]
O=Path(__file__).resolve().parent
RPC='https://mainnet.base.org'
def rpc(method,params):
    for attempt in range(7):
        time.sleep(.6)
        try:
            response=requests.post(RPC,json={'jsonrpc':'2.0','id':1,'method':method,'params':params},timeout=30)
            response.raise_for_status()
            result=response.json()
            if 'error' in result:raise RuntimeError(result['error'])
            return result['result']
        except (requests.RequestException,RuntimeError):
            if attempt==6:raise
            time.sleep(2**attempt)
def integer(x):return int(x,16) if isinstance(x,str) else int(x)

def main():
    run=json.loads((R/'mainnet04/evidence.json').read_text())
    assert run['status']=='passed'
    sources=['funding.json','pilot-cleanup.json','mainnet/evidence.json','mainnet/recovery.json',
             'mainnet02/evidence.json','mainnet03/evidence.json','mainnet03/reconciled-open-receipt.json','mainnet04/evidence.json']
    receipts={}
    locations={}
    def walk(v,origin):
        if isinstance(v,dict):
            if 'transactionHash' in v and 'gasUsed' in v and 'effectiveGasPrice' in v:
                key=v['transactionHash'].lower()
                receipts[key]=v
                locations.setdefault(key,[]).append(origin)
            for child in v.values():walk(child,origin)
        elif isinstance(v,list):
            for child in v:walk(child,origin)
    for name in sources:walk(json.loads((R/name).read_text()),name)
    out={'status':'running','rpc':RPC,'started_at_utc':datetime.now(timezone.utc).isoformat(),
         'source_files':sources,'receipts':[]}
    p=O/'canonical-receipts.json'
    if '--resume' in sys.argv and p.exists():out=json.loads(p.read_text())
    verified={v['hash'] for v in out['receipts']}
    def save():p.write_text(json.dumps(out,indent=2)+'\n')
    blocks={}
    for i,(h,old) in enumerate(receipts.items(),1):
        if h in verified:continue
        r=rpc('eth_getTransactionReceipt',[h])
        assert r and integer(r['status'])==1
        assert integer(r['blockHash'])!=0
        height=r['blockNumber']
        if height not in blocks:blocks[height]=rpc('eth_getBlockByNumber',[height,False])
        assert blocks[height]['hash'].lower()==r['blockHash'].lower()
        assert h in [v.lower() for v in blocks[height]['transactions']]
        assert integer(r['gasUsed'])==integer(old['gasUsed'])
        assert integer(r['effectiveGasPrice'])==integer(old['effectiveGasPrice'])
        total=integer(r['gasUsed'])*integer(r['effectiveGasPrice'])+integer(r['l1Fee'])
        out['receipts'].append({'hash':h,'source_files':sorted(set(locations[h])),
            'canonical_block_hash':r['blockHash'],'fee_wei':total,'receipt':r,
            'initial_l1_fee_wei':integer(old['l1Fee']),
            'l1_fee_adjustment_wei':integer(r['l1Fee'])-integer(old['l1Fee'])})
        save()
        if i%20==0:print('canonical receipts',i,'/',len(receipts),flush=True)
    funding=json.loads((R/'funding.json').read_text())
    addresses={'funding':funding['funding_address'],**funding['role_addresses'],
        'escrow_pilot':'0x9Cf0F2607a38583aEc2176d4370163C10B107f3a',
        'escrow_measurement':run['contract_address']}
    height=rpc('eth_blockNumber',[])
    balances={key:integer(rpc('eth_getBalance',[addr,height])) for key,addr in addresses.items()}
    nonce_counts={key:integer(rpc('eth_getTransactionCount',[addr,height])) for key,addr in addresses.items() if not key.startswith('escrow')}
    senders=Counter(r['receipt']['from'].lower() for r in out['receipts'])
    assert all(nonce_counts[k]==senders[a.lower()] for k,a in addresses.items() if k in nonce_counts)
    spent=sum(v['fee_wei'] for v in out['receipts'])
    initial=funding['funding_balance_wei']
    incoming_hashes=['0xf59ce9b5230ab2d50fae7bd7a9b3a1b61780ce64d961910e1473e83961f6a4d6',
                     '0x93f030a54fe44a7186a3b36d6a2cdd0beae246d699de504687a2895d62c18095']
    external=[]
    for h in incoming_hashes:
        tx=rpc('eth_getTransactionByHash',[h]);receipt=rpc('eth_getTransactionReceipt',[h])
        assert integer(receipt['status'])==1
        assert tx['to'].lower() in {a.lower() for a in addresses.values()}
        assert tx['from'].lower() not in {a.lower() for a in addresses.values()}
        external.append({'transaction':tx,'receipt':receipt,'value_wei':integer(tx['value'])})
    external_value=sum(v['value_wei'] for v in external)
    out.update(balance_block=integer(height),balances_wei=balances,transaction_counts=nonce_counts,
        addresses=addresses,initial_funding_wei=initial,all_experiment_receipt_fee_wei=spent,
        unrelated_incoming_transfers=external,unrelated_incoming_wei=external_value,
        observed_total_balance_decrease_wei=initial-sum(balances.values()),
        fee_balance_difference_wei=spent-(initial-sum(balances.values())))
    save()
    assert initial+external_value-sum(balances.values())==spent, (initial,external_value,sum(balances.values()),spent)
    run_hashes={t['transaction_hash'].lower() for t in run['transactions']}
    run_fee=sum(r['fee_wei'] for r in out['receipts'] if r['hash'] in run_hashes)
    out.update(status='passed',finished_at_utc=datetime.now(timezone.utc).isoformat(),
        receipt_count=len(receipts),balance_block=integer(height),balances_wei=balances,
        transaction_counts=nonce_counts,addresses=addresses,initial_funding_wei=initial,
        measured_run_fee_wei=run_fee,all_experiment_fee_wei=spent,
        measured_run_initial_receipt_fee_wei=run['total_fee_wei'],
        provisional_l1_fee_adjustments=sum(r['l1_fee_adjustment_wei']!=0 for r in out['receipts']),
        setup_pilot_funding_cleanup_fee_wei=spent-run_fee,
        remaining_controlled_funds_wei=sum(balances.values()),
        finality_note='Receipt block hashes checked against current canonical L2 blocks; this is not a claim of L1 finality.')
    save()
    print(json.dumps({k:v for k,v in out.items() if k!='receipts'},indent=2),flush=True)

if __name__=='__main__':main()
