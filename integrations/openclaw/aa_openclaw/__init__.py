"""OpenClaw observations enter the Python SDK here; Python owns all evidence hashes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import os
from threading import RLock

from aa_sdk import Accountability

SCOPED_TOOLS = ['write']
MATCH_KEY = 'path'
_DIRECT = '__direct__'

@dataclass
class Binding:
    acc: Accountability
    session: object
    pending: set = field(default_factory=set)
    events: dict = field(default_factory=dict)
    summary: dict | None = None
    lock: RLock = field(default_factory=RLock)

_sessions: dict[str, Binding] = {}
_registry_lock = RLock()

def scope_params():
    return {'scoped_tools': SCOPED_TOOLS, 'allow_prefixes': [os.environ.get('AA_SCOPE_PREFIX', 'workspace/')], 'match_key': MATCH_KEY}

def aggregate_params(max_count):
    return {'counted_tools': SCOPED_TOOLS, 'max_count': max_count}

def begin_session(party, store=None, chain=None, provider_addr=None, bond_wei=0,
                  payout_wei=10**16, aggregate_max_count=None, native_session_id=_DIRECT, consent_tools=None):
    if not party or not party.strip():
        raise ValueError('party is required')
    with _registry_lock:
        existing = _sessions.get(native_session_id)
        if existing and (existing.summary is None or native_session_id != _DIRECT):
            if existing.session.party != party:
                raise ValueError('native session is already bound to another party')
            return existing.acc, existing.session
        acc = Accountability('openclaw-provider', store=store, chain=chain, provider_addr=provider_addr)
        acc.register_promise('action_within_declared_scope', scope_params(), payout_wei=payout_wei)
        if native_session_id != _DIRECT:
            acc.register_promise('no_destructive_without_consent', {
                'destructive_tools':consent_tools if consent_tools is not None else ['exec'], 'authorization_mode':'invocation'}, payout_wei=payout_wei)
        if aggregate_max_count is not None:
            acc.register_promise('aggregate_within_cap', aggregate_params(aggregate_max_count), payout_wei=payout_wei)
        sess = acc.session(party=party)
        _sessions[native_session_id] = Binding(acc, sess)
        return acc, sess

def current(native_session_id=_DIRECT):
    item = _sessions.get(native_session_id)
    return (item.acc, item.session) if item else (None, None)

def is_active(native_session_id=_DIRECT):
    item = _sessions.get(native_session_id)
    return item is not None and item.summary is None

def record_action(tool, args, result, native_session_id=_DIRECT):
    item = _sessions.get(native_session_id)
    if item is None or item.summary is not None:
        raise ValueError('no active session')
    with item.lock:
        item.session.record(tool, dict(args), result)

def observe(event):
    item = _sessions.get(event['native_session_id'])
    if item is None:
        raise ValueError('no active native session')
    with item.lock:
        event_id = event['event_id']
        if event_id in item.events:
            if item.events[event_id] != event:
                raise ValueError('conflicting event replay')
            return {'seq': item.session._seq, 'reused': True}
        if item.summary is not None:
            raise ValueError('native session already finalized')
        phase, action_id = event['phase'], event['action_id']
        common = dict(event_id=event_id, action_id=action_id)
        if phase == 'execution_started':
            item.pending.add(action_id)
        elif phase == 'approval_requested':
            item.session.record('authorization_requested', {
                'request_id':event['request_id'], 'action_id':action_id,
                'tool':event['tool'], 'args':event['args'], 'origin':event.get('origin')},
                'pending', **common)
        elif phase == 'approval_resolved':
            raw = event.get('decision')
            decision = 'allow' if raw in {'allow-once','allow-always','allow'} else ('timeout' if raw in {None,'timeout'} else 'deny')
            item.session.record_authorization(**common, request_id=event['request_id'],
                tool=event['tool'], args=event['args'], decision=decision,
                scope=event['scope'], authority=event['authority'],
                native_session_id=event['native_session_id'], native_decision={'decision':raw, 'origin':event.get('origin')},
                policy_id=event.get('policy_id'))
        elif phase == 'execution_completed':
            if action_id not in item.pending:
                raise ValueError('completion has no matching execution start')
            item.session.record(event['tool'], event['args'], event.get('result'), **common,
                metadata={'native_session_id':event['native_session_id']})
            item.pending.remove(action_id)
        else:
            raise ValueError('unknown observation phase')
        item.events[event_id] = deepcopy(event)
        return {'seq':item.session._seq, 'reused':False}

def end_session(native_session_id=_DIRECT):
    item = _sessions.get(native_session_id)
    if item is None:
        return None
    with item.lock:
        if item.summary is not None:
            return item.summary if native_session_id != _DIRECT else None
        if item.pending:
            raise ValueError('native executions are unresolved; trace remains open')
        item.summary = item.session.end()
        return item.summary
