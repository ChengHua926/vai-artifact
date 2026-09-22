"""Conditional workflow experiments using the existing AAP-2 evaluator.

This module records typed inputs through a privileged controller route, then
reconstructs operands and expands declarative conditions into the shared
constraint language. It never accepts an adapter-computed compliance verdict.
The route metadata assumes a faithful harness whose controller API is not
agent-accessible; it is not cryptographic authentication of a human or source.

A recording seal is an explicit experimental completeness assertion. It does
not prove that a provider logged every real-world event. Missing required data
or an absent completion boundary raises UnresolvedEvidence, including through
direct registered evaluation; it never silently becomes a satisfied verdict.
These fixtures assess successful completed actions, excluding recorded errors
and nonexecution markers, not all possible partial effects of failed calls.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from string import Formatter

from . import constraints, policy_profiles, registry
from .ids import hash_obj
from .trace import ActionRecord, is_blocked


ROUTE = 'structured-workflow-controller-v1'
PREFIX = 'workflow_'
PREDICATE = 'action_within_declared_scope'


class UnresolvedEvidence(ValueError):
    """The trace does not establish the inputs needed for a policy decision."""


class WorkflowRecorder:
    """Privileged scripted controller, deliberately separate from agent tools.

    observe() must receive actual user decisions or authoritative service data,
    not a model's description of them. Callers must restrict this object and SDK
    metadata construction to the trusted recording path.
    """

    def __init__(self, session):
        self.session = session
        self._counter = 0

    def _event(self, tool, args, source='controller'):
        self._counter += 1
        self.session.record(tool, args, 'recorded',
                            event_id=f'{self.session.session_id}:workflow:{self._counter}',
                            metadata={'recording_route': ROUTE, 'source': source})

    def begin(self, channels):
        if not isinstance(channels, (list, tuple)) or any(
                not isinstance(c, str) or not c for c in channels):
            raise ValueError('recording channels must be explicit names')
        self._event('workflow_begin', {'channels': list(channels)})

    def observe(self, name, value, *, source, action_id=None, args=None):
        self._event('workflow_observation', {
            'name': name, 'value': copy.deepcopy(value), 'action_id': action_id,
            'args_hash': hash_obj(args) if args is not None else None}, source)

    def revoke(self, name, *, source, action_id=None, args=None):
        self._event('workflow_revoke', {
            'name': name, 'action_id': action_id,
            'args_hash': hash_obj(args) if args is not None else None}, source)

    def authorize(self, *, action_id, request_id, tool, args, decision,
                  authority='human', scope='once', **native_fields):
        self._counter += 1
        event_id = f'{self.session.session_id}:native-authorization:{self._counter}'
        self.session.record_authorization(
            event_id=event_id, action_id=action_id, request_id=request_id,
            tool=tool, args=args, decision=decision, authority=authority,
            scope=scope, **native_fields)
        observed = self.session.records[-1]
        self._event('workflow_authorization_source', {
            'event_id': event_id, 'record_hash': hash_obj(observed.to_dict())})

    def complete(self, *, task_complete=True, recording_complete=True):
        if type(task_complete) is not bool or type(recording_complete) is not bool:
            raise ValueError('completion assertions must be Boolean')
        self._event('workflow_complete', {
            'task_complete': task_complete, 'recording_complete': recording_complete,
            'covered_through_seq': self.session.records[-1].seq if self.session.records else 0})


def _owned(record, source=None):
    metadata = record.metadata or {}
    return metadata.get('recording_route') == ROUTE and (
        source is None or metadata.get('source') == source)


def _successful(record):
    return not is_blocked(record) and not (
        isinstance(record.result, dict) and 'error' in record.result)


def _seal(records, config):
    ordered = sorted(records, key=lambda r: r.seq)
    if not ordered or any(r.seq != i or r.session_id != ordered[0].session_id
                          for i, r in enumerate(ordered, 1)):
        raise UnresolvedEvidence('recording must be contiguous and belong to one session')
    begins = [r for r in ordered if r.tool == 'workflow_begin' and _owned(r, 'controller')]
    ends = [r for r in ordered if r.tool == 'workflow_complete' and _owned(r, 'controller')]
    if len(begins) != 1 or begins[0].seq != 1 or len(ends) != 1 or ends[0] != ordered[-1]:
        raise UnresolvedEvidence('missing or ambiguous recording boundary')
    end = ends[0]
    if end.args.get('recording_complete') is not True or end.args.get('covered_through_seq') != end.seq - 1:
        raise UnresolvedEvidence('recording is not attested complete through the last action')
    required = {'actions'} | {v['name'] for v in config.get('observations', {}).values()}
    if 'authorization.' in str(config):
        required.add('authorization')
    if not required.issubset(set(begins[0].args.get('channels', []))):
        raise UnresolvedEvidence('a required evidence channel was not completely recorded')
    return ordered, end


def _path(value, path):
    for segment in path.split('.') if path else []:
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and segment.isdigit() and int(segment) < len(value):
            value = value[int(segment)]
        else:
            raise UnresolvedEvidence(f'required field is unavailable: {path}')
    return value


def _authorization(prior, action):
    """Reconstruct the latest bound native decision; this is not a verdict."""
    absent = {'decision': 'missing', 'authority': None, 'policy_id': None}
    attested = {r.args.get('event_id'): r.args.get('record_hash') for r in prior
                if r.tool == 'workflow_authorization_source' and _owned(r, 'controller')}
    action_id = (action.metadata or {}).get('action_id')
    if not action_id:
        return absent
    if any(_successful(r) and not r.tool.startswith(PREFIX) and r.tool != 'user_authorization'
           and (r.metadata or {}).get('action_id') == action_id for r in prior):
        return {**absent, 'decision': 'consumed'}
    candidates = [r for r in prior if r.tool == 'user_authorization'
                  and r.args.get('action_id') == action_id
                  and attested.get((r.metadata or {}).get('event_id')) == hash_obj(r.to_dict())]
    if not candidates:
        return absent
    value = candidates[-1].args
    authority = value.get('authority')
    if not (type(value.get('schema_version')) is int and value['schema_version'] == 1
            and value.get('session_id') == action.session_id
            and value.get('tool') == action.tool and value.get('args_hash') == hash_obj(action.args)
            and value.get('scope') in {'once', 'session', 'persistent'}
            and (authority == 'human' or (authority == 'policy' and value.get('policy_id')))):
        return {**absent, **copy.deepcopy(value), 'decision': 'unbound'}
    return {**absent, **copy.deepcopy(value)}


def _context(prior, action, config):
    context = {'action': action.to_dict(), 'evidence': {}, 'collections': {},
               'authorization': _authorization(prior, action)}
    action_id = (action.metadata or {}).get('action_id')
    for alias, spec in config.get('observations', {}).items():
        candidates = [r for r in prior
                      if r.tool in {'workflow_observation', 'workflow_revoke'}
                      and _owned(r, spec['source']) and r.args.get('name') == spec['name']
                      and (not spec.get('action_bound') or
                           (action_id and r.args.get('action_id') == action_id))
                      and (not spec.get('args_bound') or r.args.get('args_hash') == hash_obj(action.args))]
        if candidates and candidates[-1].tool == 'workflow_observation':
            context['evidence'][alias] = copy.deepcopy(candidates[-1].args['value'])
    for alias, spec in config.get('collections', {}).items():
        selected = list(prior) + ([action] if spec.get('include_current', False) else [])
        values = [_path(r.to_dict(), spec['path']) for r in selected
                  if r.tool in spec['tools'] and _successful(r)]
        context['collections'][alias] = values
    return context


def _bind(node, context):
    if not isinstance(node, dict):
        return constraints.lit(node)
    if set(node) == {'ref'}:
        return constraints.fact(node['ref'], _path(context, node['ref']))
    if set(node) == {'lit'}:
        return copy.deepcopy(node)
    if set(node) != {'op', 'args'}:
        raise ValueError('conditions must use literals, references, and shared operations')
    args = [_bind(v, context) for v in node['args']]
    if node['op'] == 'subset':
        if len(args) != 2:
            raise ValueError('subset needs expected and observed sets')
        expected = constraints.evaluate(args[0])
        if not isinstance(expected, list):
            raise ValueError('subset expected operand must be a list')
        return constraints.expr('and', *[constraints.expr('in', constraints.lit(v), args[1]) for v in expected])
    if node['op'] == 'all_nonempty_fields':
        if len(args) != 2:
            raise ValueError('all_nonempty_fields needs records and required field names')
        records, fields = (constraints.evaluate(arg) for arg in args)
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError('required records must be a typed list of objects')
        if not isinstance(fields, list) or not fields or any(
                not isinstance(field, str) or not field for field in fields):
            raise ValueError('required field names must be a nonempty list of names')
        # Expand over recorded data; the existing constraint evaluator decides
        # presence and nonemptiness. No compliance Boolean is supplied here.
        checks = [constraints.expr('gt', constraints.expr('len', args[0]), 0)]
        for index in range(len(records)):
            row = constraints.expr('lookup', args[0], index)
            for field in fields:
                value = constraints.expr('if', constraints.expr('in', field, row),
                                         constraints.expr('lookup', row, field), None)
                checks.extend([constraints.expr('present', value), constraints.expr('ne', value, '')])
        return constraints.expr('and', *checks)
    return {'op': node['op'], 'args': args}


def render_typed(template, values):
    """Render exactly declared scalar fields; no free-text model completion."""
    fields = []
    for _, field, format_spec, conversion in Formatter().parse(template):
        if field is not None:
            if not field.isidentifier() or format_spec or conversion:
                raise ValueError('renderer permits only plain named scalar placeholders')
            fields.append(field)
    if set(fields) != set(values) or any(type(v) not in {str, int, float, bool} for v in values.values()):
        raise ValueError('renderer fields must exactly match declared scalar values')
    return template.format_map(values)


def render_action(fields, args):
    """Display exactly a policy-declared typed proposal as canonical JSON.

    This guarantees presentation fidelity only. A separate declared condition
    must compare the proposal's claims with independent authoritative inputs.
    It is not a classifier or a way to make agent assertions authoritative.
    """
    if not isinstance(fields, list) or any(not isinstance(v, str) or not v for v in fields):
        raise ValueError('renderer fields must be explicit names')
    if len(set(fields)) != len(fields) or set(args) != set(fields):
        raise ValueError('renderer fields must exactly match the proposal')
    return json.dumps(args, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _observe(records, config):
    ordered, terminal = _seal(records, config)
    checks = {}
    for name, rule in config['rules'].items():
        if rule.get('phase', 'action') == 'terminal':
            if terminal.args.get('task_complete') is not True:
                raise UnresolvedEvidence('terminal obligation lacks a task-completion boundary')
            candidates = [terminal]
        else:
            candidates = [r for r in ordered if r.tool in rule['tools'] and _successful(r)]
        for action in candidates:
            prior = [r for r in ordered if r.seq < action.seq]
            context = _context(prior, action, config)
            when = _bind(rule.get('when', {'lit': True}), context)
            applicable = constraints.evaluate(when)
            if type(applicable) is not bool:
                raise ValueError('applicability must evaluate to Boolean')
            if not applicable:
                continue
            for alias in rule.get('requires', []):
                _path(context, f'evidence.{alias}')
            condition = _bind(rule['condition'], context)
            if 'rendering' in rule:
                spec = rule['rendering']
                if any('action.' in str(v) or 'authorization.' in str(v) or 'collections.' in str(v)
                       for v in spec['fields'].values()):
                    raise ValueError('rendered fields must come from independent evidence or policy literals')
                fields = {k: constraints.evaluate(_bind(v, context)) for k, v in spec['fields'].items()}
                expected = render_typed(spec['template'], fields)
                condition = constraints.expr('and', condition, constraints.expr('eq',
                    constraints.fact('rendered_text', _path(action.to_dict(), spec['path'])), expected))
            if 'action_rendering' in rule:
                spec = rule['action_rendering']
                fields = spec['fields']
                if not spec['path'].startswith('result.'):
                    raise ValueError('action rendering must check the captured output')
                declared = constraints.expr('multiset_eq',
                    constraints.fact('proposal_fields', list(action.args)), fields)
                # A malformed proposal is explicitly rejected by the declared
                # field-set condition, rather than acquiring unchecked prose.
                expected = render_action(fields, action.args) if set(action.args) == set(fields) else ''
                condition = constraints.expr('and', condition, declared, constraints.expr('eq',
                    constraints.fact('captured_output', _path(action.to_dict(), spec['path'])), expected))
            checks.setdefault(action.seq, []).append({
                'arm': name, 'condition': condition, 'detail': rule.get('detail', name),
                'target': (action.metadata or {}).get('action_id', action.tool), 'allowed': []})
    return {'trace': [r.to_dict() for r in ordered], 'inputs': {'scope_checks': checks},
            'sequence_map': {r.seq: r.seq for r in ordered}}


def register_workflow(config):
    """Commit the reusable reconstruction code and declarative policy together."""
    config = copy.deepcopy(config)
    if not isinstance(config.get('rules'), dict) or not config['rules']:
        raise ValueError('workflow requires declared rules')
    for spec in config.get('observations', {}).values():
        if not spec.get('name') or not spec.get('source') or spec['source'] in {'agent', 'assistant', 'model'}:
            raise ValueError('observations require a named non-agent source')
    for rule in config['rules'].values():
        if 'condition' not in rule or rule.get('phase', 'action') not in {'action', 'terminal'}:
            raise ValueError('workflow rule requires a condition and supported phase')
        if rule.get('phase', 'action') == 'action' and not rule.get('tools'):
            raise ValueError('action rule requires declared tools')
    return policy_profiles.register(policy_profiles.ObservationProfile(
        'structured_workflow', 1, _observe, config,
        {name: 'scope' for name in config['rules']}, Path(__file__).read_text()))


def evaluate_workflow(records, params):
    """Experimental tri-state wrapper; never sends unresolved claims to settlement."""
    try:
        verdict = registry.get(PREDICATE).evaluate(records, params)
    except UnresolvedEvidence as error:
        return {'status': 'unresolved', 'verdict': None, 'issues': [str(error)]}
    return {'status': 'violated' if verdict.violated else 'satisfied', 'verdict': verdict, 'issues': []}
