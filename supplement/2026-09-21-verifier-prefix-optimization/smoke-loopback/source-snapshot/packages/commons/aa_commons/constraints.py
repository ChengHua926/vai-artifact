"""Small expression language for deterministic policy conditions.

Expressions carry named observations, literal policy values, and shared
operations. A fact name documents an operand; it does not authenticate it.
The registered evaluator must reconstruct observations from its committed trace.
No operation executes callbacks or imports application-specific checking code.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any


# Bounds are inclusive; None means no upper bound. Empty conjunctions and
# disjunctions intentionally retain Python's all([]) and any([]) identities.
_ARITIES = {
    **{op: (2, 2) for op in (
        'eq', 'ne', 'lt', 'le', 'gt', 'ge', 'in', 'not_in', 'in_norm',
        'contains', 'lookup', 'count', 'sub', 'prefix', 'multiset_eq',
    )},
    **{op: (1, 1) for op in (
        'present', 'is_empty', 'not', 'all', 'any', 'len', 'decimal', 'trim', 'sum',
    )},
    **{op: (0, None) for op in ('list', 'and', 'or', 'add', 'mul')},
    'if': (3, 3), 'max': (1, None), 'min': (1, None),
}


def lit(value: Any) -> dict:
    """Embed policy data without interpreting dictionaries as expressions."""
    return {'lit': value}


def fact(name: str, value: Any) -> dict:
    """Name an observed operand; provenance is supplied by trace reconstruction."""
    return {'fact': name, 'value': value}


def expr(op: str, *args: Any) -> dict:
    """Construct an expression, boxing values and recursively composing lists."""
    return {'op': op, 'args': [_box(arg) for arg in args]}


def _box(value: Any) -> dict:
    if isinstance(value, dict) and set(value) in ({'lit'}, {'fact', 'value'}, {'op', 'args'}):
        return value
    if isinstance(value, (list, tuple)):
        return {'op': 'list', 'args': [_box(item) for item in value]}
    if callable(value):
        raise TypeError('executable callbacks are not constraint operands')
    return lit(value)


def _validate_value(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('constraint numbers must be finite')
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('constraint numbers must be finite')
    elif value is None or isinstance(value, (str, int, bool)):
        return
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_value(key)
            _validate_value(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _validate_value(item)
    else:
        raise TypeError(f'unsupported constraint operand type: {type(value).__name__}')


def _validate_node(node: Any) -> None:
    if not isinstance(node, dict):
        raise ValueError('constraint nodes must be expression dictionaries')
    keys = set(node)
    if keys == {'lit'}:
        _validate_value(node['lit'])
        return
    if keys == {'fact', 'value'}:
        if not isinstance(node['fact'], str) or not node['fact']:
            raise ValueError('constraint fact names must be nonempty strings')
        _validate_value(node['value'])
        return
    if keys != {'op', 'args'}:
        raise ValueError('malformed constraint expression')
    op, args = node['op'], node['args']
    if not isinstance(op, str) or op not in _ARITIES:
        raise ValueError(f'unknown constraint operation: {op!r}')
    if not isinstance(args, list):
        raise ValueError('constraint args must be a list')
    minimum, maximum = _ARITIES[op]
    if len(args) < minimum or (maximum is not None and len(args) > maximum):
        raise ValueError(f'invalid argument count for constraint operation {op!r}')
    for arg in args:
        _validate_node(arg)


def _norm(value: Any) -> str:
    return re.sub(r'^https?://', '', str(value).strip().lower()).rstrip('./')


def evaluate(node: dict) -> Any:
    """Validate and evaluate a condition or value expression.

    The entire expression is validated, including unselected branches. Evaluation
    of ``and``, ``or``, and ``if`` still short-circuits, allowing presence checks
    to guard lookups of missing observations. Logical operations require Boolean
    operands instead of interpreting nonempty strings or numbers as consent. Arithmetic uses operand types,
    including Decimal for exact monetary calculations. Policy engines must require
    a Boolean result when using an expression as a compliance condition.
    """
    _validate_node(node)
    return _evaluate(node)


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError('logical constraint operands must be Boolean')
    return value


def _evaluate(node: dict) -> Any:
    if 'lit' in node:
        return node['lit']
    if 'fact' in node:
        return node['value']
    op, args = node['op'], node['args']
    if op == 'and':
        return all(_boolean(_evaluate(arg)) for arg in args)
    if op == 'or':
        return any(_boolean(_evaluate(arg)) for arg in args)
    if op == 'if':
        return _evaluate(args[1] if _boolean(_evaluate(args[0])) else args[2])
    values = [_evaluate(arg) for arg in args]
    result = _operation(op, values)
    _validate_value(result)
    return result


def _operation(op: str, values: list) -> Any:
    if op == 'list': return values
    if op == 'eq': return values[0] == values[1]
    if op == 'ne': return values[0] != values[1]
    if op == 'lt': return values[0] < values[1]
    if op == 'le': return values[0] <= values[1]
    if op == 'gt': return values[0] > values[1]
    if op == 'ge': return values[0] >= values[1]
    if op == 'in': return values[0] in values[1]
    if op == 'not_in': return values[0] not in values[1]
    if op == 'in_norm': return _norm(values[0]) in {_norm(value) for value in values[1]}
    if op == 'contains': return values[1] in values[0]
    if op == 'present': return values[0] is not None
    if op == 'is_empty': return not values[0]
    if op == 'not': return not _boolean(values[0])
    if op == 'all': return all(_boolean(value) for value in values[0])
    if op == 'any': return any(_boolean(value) for value in values[0])
    if op == 'len': return len(values[0])
    if op == 'lookup': return values[0][values[1]]
    if op == 'count': return values[0].count(values[1])
    if op == 'decimal': return Decimal(str(values[0]))
    if op == 'trim': return values[0].strip()
    if op == 'add': return sum(values)
    if op == 'sub': return values[0] - values[1]
    if op == 'mul':
        result = 1
        for value in values:
            result *= value
        return result
    if op == 'sum': return sum(values[0])
    if op == 'max': return max(values)
    if op == 'min': return min(values)
    if op == 'prefix': return values[1][:len(values[0])] == values[0]
    if op == 'multiset_eq':
        remaining = list(values[1])
        for value in values[0]:
            if value not in remaining:
                return False
            remaining.remove(value)
        return not remaining
    raise ValueError(f'unknown constraint operation: {op!r}')
