"""Source-addressed observation and policy definitions for shared predicates.

Only trusted, locally installed Python can register a profile. Promise parameters
select its exact content hash; they cannot supply code or an import path. The
profile reconstructs operands/conditions from records, and the shared engine
computes the verdict. Its policy configuration and complete helper source bundle
are transitively committed through params_hash.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Callable

from .ids import hash_obj
from .predicate import Verdict
from .trace import ActionRecord

FAMILIES = {
    'scope': 'action_within_declared_scope',
    'mandate': 'payment_within_mandate',
    'count': 'aggregate_within_cap',
}


@dataclass(frozen=True)
class ObservationProfile:
    profile_id: str
    version: int
    build: Callable[[list[ActionRecord], dict], dict]
    config: dict
    rules: dict[str, str]
    source_bundle: str
    # Names are configurable to preserve existing application reason formats.
    reason_fields: tuple[str, str, str] = ('event_seq', 'rule', 'detail')


@dataclass(frozen=True)
class RegisteredProfile:
    profile_hash: str
    descriptor_json: str
    build: Callable[[list[ActionRecord], dict], dict]

    @property
    def descriptor(self):
        # Return a fresh copy: callers cannot mutate a committed configuration.
        return json.loads(self.descriptor_json)

    def observe(self, records):
        return self.build(records, self.descriptor['config'])


_PROFILES: dict[str, RegisteredProfile] = {}


def register(spec: ObservationProfile) -> RegisteredProfile:
    if not isinstance(spec.profile_id, str) or not spec.profile_id:
        raise ValueError('profile id must be a nonempty string')
    if type(spec.version) is not int or spec.version < 1:
        raise ValueError('profile version must be a positive integer')
    if not isinstance(spec.config, dict) or not isinstance(spec.rules, dict) or not spec.rules:
        raise ValueError('profile needs configuration and declared rules')
    if any(not isinstance(rule, str) or not rule or family not in FAMILIES
           for rule, family in spec.rules.items()):
        raise ValueError('invalid profile rule or family')
    if not isinstance(spec.source_bundle, str) or not spec.source_bundle:
        raise ValueError('profile requires a complete source bundle')
    if len(spec.reason_fields) != 3 or len(set(spec.reason_fields)) != 3 or any(
            not isinstance(key, str) or not key for key in spec.reason_fields):
        raise ValueError('profile requires three distinct reason field names')
    descriptor = {'profile_id': spec.profile_id, 'version': spec.version,
                  'config': spec.config, 'rules': spec.rules,
                  'source_bundle': spec.source_bundle,
                  'build_source': inspect.getsource(spec.build),
                  'reason_fields': list(spec.reason_fields)}
    serialized = json.dumps(descriptor, sort_keys=True, allow_nan=False)
    identity = hash_obj(json.loads(serialized))
    existing = _PROFILES.get(identity)
    if existing is not None:
        if existing.build is not spec.build:
            raise ValueError('profile hash already bound to a different callable')
        return existing
    registered = RegisteredProfile(identity, serialized, spec.build)
    _PROFILES[identity] = registered
    return registered


def resolve(profile_hash: str) -> RegisteredProfile:
    if not isinstance(profile_hash, str) or profile_hash not in _PROFILES:
        raise ValueError('unknown observation profile hash; install its pinned definitions')
    return _PROFILES[profile_hash]


def validate_params(family: str, params: dict) -> RegisteredProfile:
    if not isinstance(params, dict) or set(params) != {'observation_profile', 'rule'}:
        raise ValueError('profile params must contain exactly observation_profile and rule')
    profile = resolve(params['observation_profile'])
    rule = params['rule']
    rules = profile.descriptor['rules']
    if not isinstance(rule, str) or rule not in rules:
        raise ValueError('unknown profile rule')
    if rules[rule] != family:
        raise ValueError('profile rule does not belong to this predicate family')
    return profile


def evaluate_profile(records: list[ActionRecord], params: dict, family: str) -> Verdict:
    from .policy_engine import run
    profile = validate_params(family, params)
    observed = profile.observe(records)
    if not isinstance(observed, dict) or not all(
            key in observed for key in ('trace', 'inputs', 'sequence_map')):
        raise ValueError('profile must supply trace, inputs and sequence_map')
    if 'fires' in observed or 'verdicts' in observed:
        raise ValueError('profile must supply observations and conditions, not verdicts')
    hits = [f for f in run(observed['trace'], observed['inputs']) if f.get('arm') == params['rule']]
    if not hits:
        return Verdict.satisfied()
    mapping = observed['sequence_map']
    first = min(hits, key=lambda f: mapping[f['seq']])
    record_seq = mapping[first['seq']]
    if type(record_seq) is not int or record_seq not in {r.seq for r in records}:
        raise ValueError('violation has no committed record witness')
    seq_key, rule_key, detail_key = profile.descriptor['reason_fields']
    reason = {seq_key: first['seq'], rule_key: params['rule'], detail_key: first.get('detail')}
    return Verdict.violation(record_seq, json.dumps(reason, sort_keys=True))
