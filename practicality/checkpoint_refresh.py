"""Archive-only V2 claim-flow rerun of the native Hermes/OpenClaw checkpoint matrices.

Uses unchanged current EscrowClient._send, including pending/latest nonce checks.
Mainnet: 3 policies x 3 sessions x 20 writes; pause 35 seconds after two writes.
Fee reconciliation occurs BETWEEN sessions, outside initial receipt latency.
Mainnet also requires the committed prototype revision (clean tree), the deployment written
by the V2 settlement driver, and the V2 study's fees spent before this run.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import random
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for directory in ('commons', 'sdk', 'store', 'verifier'):
    sys.path.insert(0, str(ROOT/'packages'/directory))
import requests
from web3 import Web3
from aa_commons import ActionRecord, trace_hash
from aa_commons.trace import check_prefixes
from aa_sdk import HttpStore
from aa_sdk.chain import EscrowClient


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

OC = load(ROOT/'practicality/measure_openclaw.py', 'original_openclaw_measurement')
PAYOUT = OC.PAYOUT
HERMES_REVISION = '9801de7052b9f746791f96600ea19b9399de4b13'
STRATEGIES = OC.STRATEGIES
FEE_CEILING = 500 * 10**12
FORMAL_PYTHON = (3, 12, 13)
# Methodology of the September 25 matrices; any drift refuses to run.
EXPECTED_STRATEGIES = {'10_records_or_30s': (10, 30), '30_records_or_60s': (30, 60), 'final_only': (10**9, 86400)}
EXPECTED_PAYOUT = 10**10
NATIVE_HERMES_SHA256 = '6f3e51fc0883b8e97582a7588491876366244ffbf4ec80248ac6eaa0b0711300'
ARTIFACT_PATH = 'contracts/out/Escrow.sol/Escrow.json'
# Public role addresses of the existing experiment wallets; no key material. Mainnet only:
# the funding.json named by AA_MAINNET_PUBLIC (unset, a nonexistent path).
PUBLIC = Path(os.environ.get('AA_MAINNET_PUBLIC') or 'AA_MAINNET_PUBLIC-unset')
# Keys written by the settlement driver's deployment.json (measure_handoff.py format).
DEPLOYMENT_KEYS = ('chain_id', 'address', 'deploy_block', 'artifact_sha256', 'provider_addr', 'verifier_addr', 'challenger_addr')
CURRENT_GUARD = None
ARGS = None


def dump(path, value):
    OC.dump(path, value)


# Copied from practicality/measure_handoff.py (September 25), which the V2 settlement
# driver may replace: canonical receipt fee and the build-matches-source check.
def number(value):
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


def fee_wei(receipt):
    return number(receipt["gasUsed"]) * number(receipt["effectiveGasPrice"]) + number(receipt.get("l1Fee", 0))


def validate_build(artifact):
    metadata = artifact["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    source_hash = Web3.to_hex(Web3.keccak((ROOT / "contracts/src/Escrow.sol").read_bytes()))
    if metadata["sources"]["src/Escrow.sol"]["keccak256"] != source_hash:
        raise ValueError("compiled contract does not match current Solidity source")


def require_methodology():
    if STRATEGIES != EXPECTED_STRATEGIES or PAYOUT != EXPECTED_PAYOUT:
        raise ValueError('checkpoint policies or payout differ from the September 25 matrices')
    if hashlib.sha256((HERE/'native_hermes_original.py.txt').read_bytes()).hexdigest() != NATIVE_HERMES_SHA256:
        raise ValueError('extracted NativeHermes class differs from the September 16/25 workload')


def git(*command):
    return subprocess.check_output(['git', *command], cwd=ROOT, text=True).strip()


def prototype_state():
    return {'head': git('rev-parse', 'HEAD'), 'status': git('status', '--porcelain')}


def require_frozen_revision(expected):
    """HEAD is the named commit, the tree is clean, and every hashed source is committed.

    The gitignored build artifact is bound instead to the committed Solidity source by
    validate_build, and to the deployment by its sha256 and live runtime bytecode.
    """
    if not expected:
        raise ValueError('mainnet requires --expected-revision naming the committed V2 prototype revision')
    resolved = git('rev-parse', '--verify', expected + '^{commit}')
    state = prototype_state()
    if state['head'] != resolved:
        raise ValueError(f"prototype HEAD {state['head']} is not the expected revision {resolved}")
    if state['status']:
        raise ValueError('prototype tree must be clean (no modified or untracked files)')
    tracked = set(git('ls-files').splitlines())
    uncommitted = sorted(path for path in manifest() if path != ARTIFACT_PATH and path not in tracked)
    if uncommitted:
        raise ValueError('hashed prototype sources are not committed: ' + ', '.join(uncommitted))
    validate_build(json.loads((ROOT/ARTIFACT_PATH).read_text()))
    return resolved


def runtime_versions():
    packages = {}
    for name in ('web3', 'eth-account', 'hexbytes', 'pydantic', 'requests', 'fastapi', 'uvicorn', 'agent-client-protocol'):
        try:
            package = importlib.metadata.distribution(name)
            packages[name] = {'version': package.version, 'location': str(package.locate_file(''))}
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    return {'python': sys.version, 'executable': sys.executable, 'packages': packages,
            'node': subprocess.check_output([str(OC.NODE), '--version'], text=True).strip()}


def require_formal_runtime(network):
    if network == 'mainnet' and tuple(sys.version_info[:3]) != FORMAL_PYTHON:
        raise ValueError('formal native costs require the original Python 3.12.13 runtime')


def workload(smoke):
    """(sessions per policy, writes, pause after two writes, inter-write seconds)."""
    return (1,3,.1,.01) if smoke else (3,20,35,.25)


def schedule(args):
    # Every policy, every repetition: no single-policy or replacement cohorts in this study.
    jobs=[(rep,name) for rep in range(args.repetitions) for name in STRATEGIES]
    random.Random(20260916 if args.harness=='hermes' else 20260924).shuffle(jobs)
    return jobs


def archive_manifest():
    return {name: hashlib.sha256((HERE/name).read_bytes()).hexdigest()
            for name in ('checkpoint_refresh.py', 'native_hermes_original.py.txt')}


def validate_native_sources():
    for name,expected in (('hermes-agent',HERMES_REVISION),('openclaw',OC.OPENCLAW_REVISION)):
        repo=ROOT.parent/name
        actual=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
        dirty=subprocess.check_output(['git','status','--porcelain'],cwd=repo,text=True).strip()
        if actual!=expected or dirty:
            raise ValueError('native harness must match its clean pinned revision: '+name)


def manifest():
    selected = []
    for prefix in ('packages/sdk/aa_sdk', 'packages/commons/aa_commons', 'packages/verifier/aa_verifier',
                   'integrations/hermes/aa_hermes', 'integrations/openclaw/aa_openclaw',
                   'integrations/openclaw/aa_helper', 'integrations/openclaw/plugin/src', 'practicality'):
        selected += [p for p in (ROOT/prefix).rglob('*') if p.is_file() and p.suffix in ('.py', '.ts', '.json')]
    selected += [ROOT/'packages/store/app.py', ROOT/'contracts/src/Escrow.sol', ROOT/'contracts/out/Escrow.sol/Escrow.json', ROOT/'scripts/_config.py']
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(selected))}


def snapshot(args):
    hashes = manifest()
    for relative in hashes:
        target = args.output/'source'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT/relative).read_bytes())
    for name in ('checkpoint_refresh.py', 'native_hermes_original.py.txt'):
        (args.output/'source'/name).write_bytes((HERE/name).read_bytes())
    # Capture tracked native Python/TypeScript sources without node_modules or private state.
    validate_native_sources()
    for name in ('hermes-agent','openclaw'):
        repo = ROOT.parent/name
        for relative in subprocess.check_output(['git','ls-files'],cwd=repo,text=True).splitlines():
            src=repo/relative
            if src.is_file() and src.suffix in ('.py','.ts','.tsx','.js','.mjs','.json'):
                target=args.output/'source/native'/name/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(src.read_bytes())
    state=prototype_state()
    dump(args.output/'manifest.json', {'status':'prepared','started_at_utc':OC.utc(),
        'study':'V2 claim flow (verifier-owned evidence deadline): checkpoint-policy matrix rerun with the September 25 methodology',
        'protocol_source_sha256':hashes, 'archive_source_sha256':archive_manifest(),
        'harness':args.harness, 'network':args.network,
        'expected_revision':args.expected_revision,'prototype_git_status':state['status'],
        'deployment_source':str(args.deployment) if args.deployment else None,
        'prior_experiment_fees_wei':args.prior_fees_wei, 'global_planning_ceiling_wei':FEE_CEILING,
        'prior_fees_scope':'mainnet fees already spent in the V2 claim-flow study before this run: the settlement '
                           'driver total (deployment and funding included), plus the first harness all_transaction_fee_wei '
                           'for the second harness; studies before V2 are excluded',
        'workload':{'policies':STRATEGIES,'schedule':schedule(args),
                    'sessions_per_policy':args.repetitions,'writes_per_session':args.actions,
                    'pause_after_writes':2,'pause_seconds':args.pause,'inter_write_seconds':args.interval,
                    'payload': {'hermes': 'original prefix native experiment fixture {index}\\n followed by 1024 x characters',
                                'hermes_bytes_per_write': [len(f'native experiment fixture {i}\n'.encode()) + 1024 for i in range(args.actions)],
                                'openclaw_bytes_per_write': 1024}},
        'receipt_latency':'first SDK receipt observation; may be a Base preconfirmation',
        'fees':'canonical L2 receipt execution fee plus l1Fee, reconciled between sessions',
        'guard':'cache conservative L1 allowance and available balance between sessions; no guard RPC inside _send',
        'prototype_revision':state['head'],
        'runtime':runtime_versions(),'platform':platform.platform(),
        'rpc_endpoint':OC.RPC if args.network=='mainnet' else args.rpc_url,
        'rpc_min_interval_seconds':.25 if args.network=='mainnet' else 0,
        'store_endpoint':args.store_url,'helper_endpoint':args.helper_url if args.harness=='openclaw' else None,
        'native_revisions':{'hermes':HERMES_REVISION,'openclaw':OC.OPENCLAW_REVISION},
        'fee_accounting':{'execution':'gasUsed * effectiveGasPrice','l1':'canonical receipt l1Fee',
                          'operator':'requires zero scalar and constant in L1Block before any signing'}})
    return hashes


def read_deployment(path):
    d=json.loads(Path(path).read_text())
    missing=[key for key in DEPLOYMENT_KEYS if key not in d]
    if missing: raise ValueError('deployment file lacks required keys: '+', '.join(missing))
    return d


def deployment(w3):
    d=read_deployment(ARGS.deployment)
    artifact_bytes=(ROOT/ARTIFACT_PATH).read_bytes()
    artifact=json.loads(artifact_bytes)
    validate_build(artifact)
    if d['chain_id'] != 8453 or int(w3.eth.chain_id) != 8453:
        raise ValueError('expected Base mainnet deployment')
    if hashlib.sha256(artifact_bytes).hexdigest()!=d['artifact_sha256']:
        raise ValueError('deployment artifact mismatch')
    code=bytes.fromhex(artifact['deployedBytecode']['object'].removeprefix('0x'))
    if bytes(w3.eth.get_code(d['address'])) != code:
        raise ValueError('runtime differs from supplied new deployment')
    contract=w3.eth.contract(address=d['address'],abi=artifact['abi'])
    verifier=d['verifier_addr']
    if contract.functions.owner().call()!=verifier or contract.functions.verifier().call()!=verifier:
        raise ValueError('operator differs from deployment')
    oracle=w3.eth.contract(address='0x4200000000000000000000000000000000000015',abi=[
        {'name':name,'type':'function','stateMutability':'view','inputs':[],
         'outputs':[{'type':'uint32' if name.endswith('Scalar') else 'uint64'}]}
        for name in ('operatorFeeScalar','operatorFeeConstant')])
    scalar,constant=int(oracle.functions.operatorFeeScalar().call()),int(oracle.functions.operatorFeeConstant().call())
    if scalar or constant: raise ValueError('operator fees changed; accounting must be updated before signing')
    public=json.loads(PUBLIC.read_text())
    for role in ('provider','challenger','verifier'):
        if d[role+'_addr'].lower()!=public['role_addresses'][role].lower():
            raise ValueError('deployment role differs from authorized existing wallets')
    dump(ARGS.output/'deployment-check.json',{'checked_at_utc':OC.utc(),'block':int(w3.eth.block_number),
        'rpc_endpoint':w3.provider.endpoint_uri,'deployment_source':str(ARGS.deployment),
        'contract_address':d['address'],'chain_id':8453,'artifact_sha256':hashlib.sha256(artifact_bytes).hexdigest(),
        'runtime_sha256':hashlib.sha256(code).hexdigest(),'runtime_matches':True,'build_matches_source':True,
        'operator_fee_scalar':scalar,'operator_fee_constant':constant,
        'operator_fee_contract':'0x4200000000000000000000000000000000000015',
        'l1_fee_oracle':'0x420000000000000000000000000000000000000F',
        'deploy_block':d['deploy_block'],'deployment_source_revision':d.get('source_revision'),
        'expected_revision':ARGS.expected_revision,
        'roles':{role:d[role+'_addr'] for role in ('provider','verifier','challenger')}})
    (ARGS.output/'source/reused-deployment.json').write_bytes(ARGS.deployment.read_bytes())
    return public,d,artifact


class GuardedSigner:
    def __init__(self, account, contract, journal):
        global CURRENT_GUARD
        self.account,self.address,self.contract,self.journal=account,account.address,Web3.to_checksum_address(contract),journal
        self.w3=OC.mainnet()
        self.allowance=None
        self.balance=0
        CURRENT_GUARD=self

    def reconcile(self):
        # Called before session opening and after all sessions, never from timed _send.
        receipts=self.journal.data.setdefault('canonical_receipts',{})
        for row in self.journal.data['signed_transactions']:
            if row['hash'] in receipts: continue
            receipt=self.w3.eth.get_transaction_receipt(row['hash'])
            for _ in range(60):
                if bytes(receipt.blockHash)!=bytes(32): break
                time.sleep(.5)
                receipt=self.w3.eth.get_transaction_receipt(row['hash'])
            if bytes(receipt.blockHash)==bytes(32) or 'l1Fee' not in receipt:
                raise RuntimeError('receipt not canonical with L1 fee; stop and reconcile journal')
            if int(receipt.status)!=1: raise RuntimeError('transaction reverted; stop experiment')
            receipts[row['hash']]=OC.jsonable(receipt)
        spent=sum(fee_wei(receipt) for receipt in receipts.values())
        if ARGS.prior_fees_wei+spent>FEE_CEILING:
            raise RuntimeError('realized experiment fees exceed shared ceiling')
        self.journal.data['canonical_fee_wei']=spent
        self.journal.write()
        oracle=self.w3.eth.contract(address='0x420000000000000000000000000000000000000F',abi=[
            {'name':'getL1FeeUpperBound','type':'function','stateMutability':'view',
             'inputs':[{'name':'size','type':'uint256'}],'outputs':[{'type':'uint256'}]}])
        self.allowance=2*int(oracle.functions.getL1FeeUpperBound(4096).call())+5*10**12
        self.balance=self.w3.eth.get_balance(self.address)
        self.journal.data['guard_refresh']={'at_utc':OC.utc(),'l1_allowance_wei':self.allowance,
                                           'balance_wei':self.balance,'signed_count':len(self.journal.data['signed_transactions'])}
        self.journal.write()

    def sign_transaction(self, tx):
        if self.allowance is None: raise RuntimeError('fee guard must be refreshed before session')
        if int(tx.get('chainId',0))!=8453 or Web3.to_checksum_address(tx['to'])!=self.contract:
            raise ValueError('wrong chain or transaction recipient')
        allowed=('registerPromise(bytes32,bytes32,uint256)','openSession(bytes32,address)',
                 'checkpointTrace(bytes32,uint256,bytes32)','commitTrace(bytes32,bytes32)')
        selectors={Web3.to_hex(Web3.keccak(text=sig)[:4]) for sig in allowed}
        data=Web3.to_hex(tx['data']) if isinstance(tx['data'],bytes) else tx['data']
        price=int(tx.get('maxFeePerGas',tx.get('gasPrice',0)))
        gas,value=int(tx['gas']),int(tx.get('value',0))
        if data[:10] not in selectors or not 0<gas<=350000 or not 0<price<=OC.MAX_GAS_PRICE or value>PAYOUT:
            raise ValueError('transaction outside bounded checkpoint experiment')
        with self.journal.lock:
            rows=self.journal.data['signed_transactions']
            observed={row['receipt']['transactionHash'].lower() for row in self.journal.data['transactions']}
            if any(row['hash'].lower() not in observed for row in rows):
                raise RuntimeError('unresolved signed transaction; reconcile before any new signing')
            receipts=self.journal.data.get('canonical_receipts',{})
            pending=[r for r in rows if r['hash'] not in receipts]
            reserved=sum(r['fee_upper_wei'] for r in pending)
            fee_upper=gas*price+self.allowance
            spent=self.journal.data.get('canonical_fee_wei',0)
            if ARGS.prior_fees_wei+spent+reserved+fee_upper>FEE_CEILING or len(rows)>=180:
                raise ValueError('shared experiment fee or transaction ceiling exceeded')
            if self.balance<=sum(r['fee_upper_wei']+r['value_wei'] for r in pending)+fee_upper+value:
                raise ValueError('provider balance does not cover conservative session costs')
            signed=self.account.sign_transaction(tx)
            if len(signed.raw_transaction)>4096: raise ValueError('transaction exceeds L1 allowance size')
            rows.append({'hash':Web3.to_hex(Web3.keccak(signed.raw_transaction)),'sender':self.address,
                'nonce':tx['nonce'],'to':tx['to'],'value_wei':value,'gas_limit':gas,
                'max_fee_per_gas':price,'l1_fee_allowance_wei':self.allowance,'fee_upper_wei':fee_upper,'signed_at_utc':OC.utc()})
            self.journal.write()
            return signed


class MeasuredChain(OC.MeasuredChain):
    def _send(self, fn, sender, value=0):
        # Keep signing through observed-receipt persistence serial. The SDK releases
        # its sender lock before the outer measurement journal is updated.
        lock=self.__dict__.setdefault('_journal_send_lock',threading.RLock())
        with lock: return self._send_measured(fn,sender,value)

    def _send_measured(self, fn, sender, value=0):
        # Any nonce wait remains inside the measured operation. Never retry an unknown broadcast.
        if 'provider_balance_before_wei' not in self.journal.data:
            self.journal.data['provider_balance_before_wei']=self.w3.eth.get_balance(sender)
            self.journal.write()
        started=time.perf_counter();wall_started=time.time()
        for attempt in range(60):
            try:
                receipt=super()._send(fn,sender,value)
                with self.journal.lock:
                    row=next(r for r in self.journal.data['transactions'] if r['receipt']['transactionHash']==OC.jsonable(receipt)['transactionHash'])
                    row['sdk_last_attempt_seconds']=row['seconds']
                    row['seconds']=time.perf_counter()-started
                    row['started_at_unix']=wall_started
                    self.journal.write()
                return receipt
            except RuntimeError as error:
                if fn.fn_name not in {'registerPromise','openSession'} or 'pending transaction; reconcile' not in str(error) or attempt==59: raise
                time.sleep(.5)


@contextmanager
def store_service(args, with_anvil=False):
    children=[]
    logs=[]
    for port in (args.store_port, *([args.helper_port] if args.harness == 'openclaw' else []),
                 *([args.anvil_port] if with_anvil else [])):
        with socket.socket() as probe: probe.bind(('127.0.0.1', port))
    commands=[([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(args.store_port),'--no-access-log'],
               ROOT/'packages/store',{'STORE_DB':str(args.output/'store.sqlite')},'store')]
    if with_anvil:
        commands.append(([str(Path.home()/'.foundry/bin/anvil'),'--host','127.0.0.1','--port',str(args.anvil_port),'--silent'],ROOT,{},'anvil'))
    try:
        for command,cwd,env,name in commands:
            log=open(args.output/(name+'.log'),'w');logs.append(log)
            children.append(subprocess.Popen(command,cwd=cwd,env={**os.environ,**env},stdout=log,stderr=subprocess.STDOUT))
        for _ in range(100):
            try:
                if requests.get(args.store_url+'/health',timeout=.5).ok and (not with_anvil or Web3(Web3.HTTPProvider(args.rpc_url)).is_connected()): break
            except requests.RequestException: pass
            if any(child.poll() is not None for child in children): raise RuntimeError('service exited')
            time.sleep(.1)
        else: raise RuntimeError('services failed to start')
        if requests.get(args.store_url+'/inventory',timeout=2).status_code!=401: raise AssertionError('store is not private')
        assert HttpStore(args.store_url).inventory()=={'promise_count':0,'record_count':0}
        yield
    finally:
        for child in children: child.terminate()
        for child in children:
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: child.kill();child.wait()
        for log in logs: log.close()


def serve(args):
    # Reuse the original native helper/configure/snapshot endpoints and exact driver.
    OC.validate_existing=deployment
    OC.LimitedSigner=GuardedSigner
    OC.MeasuredChain=MeasuredChain
    sys.path.insert(0,str(ROOT/'integrations/openclaw'))
    import aa_openclaw
    original=aa_openclaw.begin_session
    def begin(*positional,**kwargs):
        if CURRENT_GUARD: CURRENT_GUARD.reconcile()
        return original(*positional,**kwargs)
    aa_openclaw.begin_session=begin
    OC.serve(args)


def hermes(args):
    native_globals=globals().copy()
    exec(compile((HERE/'native_hermes_original.py.txt').read_text(),str(HERE/'native_hermes_original.py.txt'),'exec'),native_globals)
    native=native_globals['NativeHermes'](ROOT.parent/'hermes-agent',args.output/'fixtures')
    journal=OC.Journal(args.output/'chain-journal.json',{'transactions':[],'signed_transactions':[]})
    if args.network=='mainnet':
        w3=OC.mainnet();public,d,artifact=deployment(w3)
        provider,party=d['provider_addr'],d['challenger_addr']
        account=OC.private_account(args.keystore_dir/'provider.json',args.keystore_dir/'provider.password',provider)
        signer=GuardedSigner(account,d['address'],journal)
        chain=MeasuredChain(w3,d['address'],artifact['abi'],{provider:signer})
    else:
        w3=Web3(Web3.HTTPProvider(args.rpc_url));provider,party=w3.eth.accounts[1:3]
        deployed=EscrowClient.deploy(w3,str(OC.ARTIFACT),w3.eth.accounts[3],w3.eth.accounts[3])
        chain=MeasuredChain(w3,deployed.contract.address,json.loads(OC.ARTIFACT.read_text())['abi'])
    chain.journal=journal
    journal.data.update(contract_address=chain.contract.address,chain_id=int(w3.eth.chain_id),provider=provider,
                        provider_balance_before_wei=w3.eth.get_balance(provider));journal.write()
    store=HttpStore(args.store_url)
    rows=[]
    try:
        for rep,strategy in schedule(args):
            if CURRENT_GUARD: CURRENT_GUARD.reconcile()
            name=f'checkpoint-{args.output.name}-{strategy}-{rep}'
            acc,session=native.begin(name,store,chain,provider,party,STRATEGIES[strategy])
            start=time.perf_counter();effects=[]
            for index in range(args.actions):
                if index==2: time.sleep(args.pause)
                effects.append(native.write(name,index))
                time.sleep(args.interval)
            action_seconds=time.perf_counter()-start
            final_start=time.perf_counter();summary=native.end(name);final_seconds=time.perf_counter()-final_start
            if session._worker:
                session._worker.join(timeout=5)
                if session._worker.is_alive(): raise RuntimeError('checkpoint worker still active')
            records=store.get_records(session.session_id);parsed=[ActionRecord.from_dict(r) for r in records]
            checkpoints=chain.get_checkpoints(session.session_id);check_prefixes(parsed,checkpoints,session.session_id)
            state=chain.get_session(session.session_id)
            assert len(records)==args.actions*4 and Web3.to_hex(state[2])==trace_hash(parsed)==summary['trace_hash']
            assert all(not v.violated for v in acc.self_check(parsed).values())
            if strategy=='final_only': assert not checkpoints
            row={'name':name,'strategy':strategy,'repetition':rep,'session_id':session.session_id,'records':records,
                 'checkpoints':checkpoints,'trace_hash':summary['trace_hash'],'tool_effects':effects,
                 'action_span_seconds':action_seconds,'finalization_seconds':final_seconds,
                 'transactions':[r for r in journal.data['transactions'] if r['session_id']==session.session_id]}
            rows.append(row);dump(args.output/'native-results.json',{'checkpoint_runs':rows})
            print('completed',strategy,rep,flush=True)
        if CURRENT_GUARD: CURRENT_GUARD.reconcile()
    finally:
        native.loop.call_soon_threadsafe(native.loop.stop);native.thread.join(timeout=5)


def serve_command(args):
    # The helper child re-validates every mainnet requirement in main(); pass each one through.
    command=[sys.executable,str(Path(__file__)),'serve','--harness','openclaw','--network',args.network,'--output',str(args.output),
             '--store-port',str(args.store_port),'--helper-port',str(args.helper_port),'--anvil-port',str(args.anvil_port),
             '--prior-fees-wei',str(args.prior_fees_wei)]
    if args.deployment: command+=['--deployment',str(args.deployment)]
    if args.keystore_dir: command+=['--keystore-dir',str(args.keystore_dir)]
    if args.expected_revision: command+=['--expected-revision',args.expected_revision]
    if args.network=='mainnet': command+=['--execute-mainnet']
    return command


def openclaw(args):
    with open(args.output/'helper.log','w') as log:
        command=serve_command(args)
        child=subprocess.Popen(command,cwd=ROOT,env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(1800):
                if child.poll() is not None: raise RuntimeError('OpenClaw helper exited')
                try:
                    if requests.get(args.helper_url+'/health',timeout=.5).ok: break
                except requests.RequestException: pass
                time.sleep(.1)
            else: raise RuntimeError('OpenClaw helper not ready')
            d=json.loads(args.deployment.read_text()) if args.network=='mainnet' else None
            party=d['challenger_addr'] if d else '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC'
            jobs=[]
            for rep,strategy in schedule(args):
                name=f'checkpoint-{args.output.name}-{strategy}-{rep}'
                n,seconds=STRATEGIES[strategy]
                jobs.append({'helper_url':args.helper_url,'helper_timeout_ms':120000,'party':party,'session_id':name,
                    'directory':str(args.output/'native-files'/name),'writes':args.actions,'payload_bytes':1024,
                    'enabled':True,'mode':'http_store_anvil','repetition':rep,'warmup':False,'strategy':strategy,
                    'checkpoint_records':n,'checkpoint_seconds':seconds,'pause_after':2,'pause_seconds':args.pause,
                    'inter_write_seconds':args.interval})
            dump(args.output/'jobs.json',{'jobs':jobs})
            command=[str(OC.NODE),str(OC.OPENCLAW/'node_modules/vitest/vitest.mjs'),'run','--config',str(ROOT/'practicality/openclaw_native_driver.config.ts')]
            env={**os.environ,'PATH':str(OC.NODE.parent)+os.pathsep+os.environ['PATH'],
                 'AA_MEASUREMENT_JOB':str(args.output/'jobs.json'),'AA_MEASUREMENT_OUTPUT':str(args.output/'native-results.json')}
            with open(args.output/'native-driver.log','w') as driverlog:
                subprocess.run(command,cwd=OC.OPENCLAW,env=env,stdout=driverlog,stderr=subprocess.STDOUT,check=True)
        finally:
            child.terminate()
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: child.kill();child.wait()


def canonical_and_summary(args):
    journal=json.loads((args.output/'chain-journal.json').read_text())
    w3=OC.mainnet() if args.network=='mainnet' else Web3(Web3.HTTPProvider(args.rpc_url))
    receipts={}
    if args.network=='mainnet':
        assert {r['hash'].lower() for r in journal['signed_transactions']}=={r['receipt']['transactionHash'].lower() for r in journal['transactions']}, 'unresolved signed transaction; reconcile before reporting'
    for tx in journal['transactions']:
        key=tx['receipt']['transactionHash'].lower()
        receipt=w3.eth.get_transaction_receipt(key)
        for _ in range(60):
            if bytes(receipt.blockHash)!=bytes(32): break
            time.sleep(.5);receipt=w3.eth.get_transaction_receipt(key)
        assert bytes(receipt.blockHash)!=bytes(32) and int(receipt.status)==1
        if args.network=='mainnet': assert 'l1Fee' in receipt
        receipts[key]=OC.jsonable(receipt)
    dump(args.output/'canonical-receipts.json',{'receipts':receipts,'at_utc':OC.utc()})
    fee=lambda tx:fee_wei(receipts[tx['receipt']['transactionHash'].lower()])
    total=sum(fee(tx) for tx in journal['transactions'])
    last_block=max(int(r['blockNumber'],16) if isinstance(r['blockNumber'],str) else int(r['blockNumber']) for r in receipts.values())
    after=w3.eth.get_balance(journal['provider'],block_identifier=last_block)
    values=sum(tx['value_wei'] for tx in journal['transactions'])
    assert journal['provider_balance_before_wei']-after==total+values, 'provider balance conservation failed'
    if args.network=='mainnet' and total+args.prior_fees_wei>FEE_CEILING: raise RuntimeError('fee ceiling exceeded')
    native=json.loads((args.output/'native-results.json').read_text())
    rows=native['checkpoint_runs'] if args.harness=='hermes' else [
        {**r['helper_snapshot'],'strategy':r['strategy'],'repetition':r['repetition']} for r in native['jobs']]
    sessions=[]
    for row in rows:
        commits=[r for r in row['transactions'] if r['operation'] in ('checkpointTrace','commitTrace')]
        assert sum(r['operation']=='commitTrace' for r in commits)==1
        waits=[min(tx['observed_at_unix'] for tx in commits if tx['operation']=='commitTrace' or tx['record_count']>=record['seq'])-record['ts']/1000 for record in row['records']]
        assert min(waits)>=0
        sessions.append({'session_id':row['session_id'],'strategy':row['strategy'],'repetition':row['repetition'],
            'record_count':len(waits),'checkpoint_count':len(row['checkpoints']),'record_waits_seconds':waits,
            'mean_record_wait_seconds':statistics.mean(waits),'max_record_wait_seconds':max(waits),
            'commitment_fee_wei':sum(fee(tx) for tx in commits)})
    policies={}
    for strategy in STRATEGIES:
        group=[r for r in sessions if r['strategy']==strategy];assert len(group)==args.repetitions
        by_repetition=sorted(group,key=lambda r:r['repetition'])
        policies[strategy]={'sessions':len(group),'checkpoint_counts':[r['checkpoint_count'] for r in group],
            'mean_commitment_fee_microeth':statistics.mean(r['commitment_fee_wei'] for r in group)/10**12,
            'mean_record_wait_seconds':statistics.mean(r['mean_record_wait_seconds'] for r in group),
            'max_record_wait_seconds':max(r['max_record_wait_seconds'] for r in group),
            'mean_of_per_run_max_record_wait_seconds':statistics.mean(r['max_record_wait_seconds'] for r in group),
            'per_run':[{'repetition':r['repetition'],'session_id':r['session_id'],'checkpoint_count':r['checkpoint_count'],
                        'mean_record_wait_seconds':r['mean_record_wait_seconds'],
                        'max_record_wait_seconds':r['max_record_wait_seconds'],
                        'commitment_fee_wei':r['commitment_fee_wei']} for r in by_repetition]}
    result={'harness':args.harness,'network':args.network,'sessions':sessions,'policies':policies,
        'wait_statistics':{'unit':'seconds from record timestamp to the first observed receipt of the first commitment covering it',
            'mean_record_wait_seconds':'mean over runs of each run mean (table wait column)',
            'max_record_wait_seconds':'overall maximum over every record of every run',
            'mean_of_per_run_max_record_wait_seconds':'mean over runs of each run maximum (text)',
            'per_run':'one entry per run in repetition order (figure dots)'},
        'all_transaction_count':len(journal['transactions']),'all_transaction_fee_wei':total,
        'provider_balance_before_wei':journal['provider_balance_before_wei'],'provider_balance_after_wei':after,
        'reserve_deposits_wei':values,
        'global_experiment_fees_so_far_wei':args.prior_fees_wei+total,
        'operation_counts':dict(Counter(tx['operation'] for tx in journal['transactions'])),
        'first_receipts_with_zero_block_hash':sum(int(tx['receipt']['blockHash'],16)==0 for tx in journal['transactions'])}
    dump(args.output/'summary.json',result)
    return result


def check_arguments(args):
    """Refuse incomplete mainnet requests before any service, RPC or key access."""
    if Path(args.output).resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('write experiment outputs outside the prototype repository')
    if args.network=='mainnet':
        if not args.execute_mainnet or args.smoke or not args.deployment or not args.keystore_dir:
            raise ValueError('mainnet requires --execute-mainnet, --deployment and --keystore-dir, without --smoke')
        if args.prior_fees_wei is None or not 0<=args.prior_fees_wei<FEE_CEILING:
            raise ValueError('mainnet requires --prior-fees-wei: V2-study mainnet fees already spent (0 or more, below the ceiling)')
        read_deployment(args.deployment)
    elif args.prior_fees_wei is None:
        args.prior_fees_wei=0


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=('run','serve'))
    p.add_argument('--harness',choices=('hermes','openclaw'),default='openclaw')
    p.add_argument('--network',choices=('local','mainnet'),default='local')
    p.add_argument('--output',type=Path,required=True,help='fresh directory outside the prototype repository')
    p.add_argument('--deployment',type=Path,help='deployment.json written by the V2 settlement driver (mainnet)')
    p.add_argument('--keystore-dir',type=Path)
    p.add_argument('--expected-revision',help='committed prototype revision; mainnet requires HEAD to equal it and a clean tree')
    p.add_argument('--prior-fees-wei',type=int,help='mainnet fees already spent in the V2 study before this run (required on mainnet; may be 0)')
    p.add_argument('--execute-mainnet',action='store_true')
    p.add_argument('--smoke',action='store_true')
    p.add_argument('--store-port',type=int,default=19361)
    p.add_argument('--helper-port',type=int,default=19362)
    p.add_argument('--anvil-port',type=int,default=19363)
    return p


def main():
    global ARGS
    args=ARGS=parser().parse_args()
    require_formal_runtime(args.network)
    require_methodology()
    args.output=args.output.resolve();args.store_url=f'http://127.0.0.1:{args.store_port}'
    args.helper_url=f'http://127.0.0.1:{args.helper_port}';args.rpc_url=f'http://127.0.0.1:{args.anvil_port}'
    args.repetitions,args.actions,args.pause,args.interval=workload(args.smoke)
    check_arguments(args)
    if args.network=='mainnet' or args.expected_revision:
        args.expected_revision=require_frozen_revision(args.expected_revision)
    if args.mode=='serve': return serve(args)
    if args.output.exists(): raise ValueError('use a fresh output directory')
    args.output.mkdir(parents=True)
    os.environ.setdefault('STORE_TOKEN',secrets.token_urlsafe(32))
    hashes=snapshot(args)
    with store_service(args,with_anvil=args.network=='local'):
        (hermes if args.harness=='hermes' else openclaw)(args)
        result=canonical_and_summary(args)
    if manifest()!=hashes: raise RuntimeError('prototype source changed during experiment')
    if args.expected_revision and prototype_state()!={'head':args.expected_revision,'status':''}:
        raise RuntimeError('prototype revision or working tree changed during experiment')
    validate_native_sources()
    meta=json.loads((args.output/'manifest.json').read_text());meta.update(status='completed',finished_at_utc=OC.utc(),source_unchanged_during_run=True)
    if archive_manifest()!=meta['archive_source_sha256']:
        raise RuntimeError('archive runner source changed during experiment')
    if (args.output/'deployment-check.json').exists():
        meta['deployment_check']=json.loads((args.output/'deployment-check.json').read_text())
    dump(args.output/'manifest.json',meta)
    print(json.dumps({'harness':args.harness,'fees_wei':result['all_transaction_fee_wei'],'policies':result['policies']}),flush=True)


if __name__=='__main__': main()
