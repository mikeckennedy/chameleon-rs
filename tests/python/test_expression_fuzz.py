"""Generated TALES expressions evaluated by both engines in the same scope: the same value
(type and repr) or the same exception type and arguments.

The generator is biased toward the fast path's node types (names, attributes, constants,
calls, `not`, subscripts, comparisons, conditional and boolean expressions) and toward the
boundaries where the fast path hands over to the fallback: comprehensions, lambdas, walrus,
starred arguments, `**` dicts, f-strings, slices, chained comparisons, keyword arguments,
attribute-versus-item lookup, builtins shadowed by render keywords, `econtext`, `__` names,
TALES prefixes and pipes. CHAMELEON_RS_FUZZ_EXAMPLES raises the example count (default 600).
"""

from __future__ import annotations

import os
import re
from typing import Any

import chameleon
import chameleon.tal
import chameleon_rs
import pytest
from exprtest import evaluate
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

EXAMPLES = int(os.environ.get('CHAMELEON_RS_FUZZ_EXAMPLES', '600'))


class Obj:
    x = 1
    s = 'str<'

    def m(self, y: Any = 0) -> Any:
        return ('m', y)

    def __repr__(self) -> str:
        return 'Obj()'


class ItemOnly:
    """Attribute lookups fall back to `__getitem__`; a KeyError keeps the AttributeError."""

    def __getitem__(self, key: Any) -> Any:
        if key in ('k', 0):
            return 'item'
        if key == 'boom':
            raise ValueError('boom')
        raise KeyError(key)

    def __repr__(self) -> str:
        return 'ItemOnly()'


def f(*args: Any, **kwargs: Any) -> Any:
    return (args, sorted(kwargs.items()))


SCOPE: dict[str, Any] = {
    'a': 3,
    'b': '<x>',
    'c': [1, 2, 3],
    'd': {'k': 'v', 'n': 0, 'boom': 1},
    'o': Obj(),
    'g': ItemOnly(),
    'n': None,
    'f': f,
    'z': 0,
}

# Render keywords that shadow builtins (`get('len', len)` finds these first).
SHADOWS: dict[str, Any] = {'max': lambda *a: 'shadowed max', 'str': repr}

ATOMS = st.sampled_from(
    [
        'a', 'b', 'c', 'd', 'o', 'g', 'n', 'f', 'z', 'missing', 'len', 'str', 'max', 'sorted', 'nothing',
        'rcontext', 'repeat', 'econtext["a"]', '"a" in econtext',
        '[k for k in econtext if k == "a"]', 'econtext.get("missing", 1)', 'True', 'None', '0', '1', '-2', '1.5', "''", "'a'", "'<'", "'%s'",
        '__builtins__["len"]', '__x', 'translate', 'target_language', 'default', 'attrs', '...',
    ]
)  # fmt: skip

UNARY = ['not {}', '-{}', '+{}', '~{}', '({})']
BINARY = ['{} + {}', '{} - {}', '{} * {}', '{} / {}', '{} // {}', '{} % {}', '{} ** {}', '{} & {}', '{} ^ {}',
          '{} << {}', '{} >> {}', '{} @ {}', '{} and {}', '{} or {}', '{} < {}', '{} <= {} < {}', '{} == {}',
          '{} != {}', '{} in {}', '{} not in {}', '{} if {} else {}', '{}[{}]',
          '{}[{}:{}]', '{}[::{}]', '{}[{}, {}]', 'f({}, k={})', 'max({}, {})', '({}, {})', '[{}, {}]',
          '{{{}: {}}}', '{} | {}']  # fmt: skip
ONE = ['{}.x', '{}.s', '{}.k', '{}.boom', '{}.missing', '{}.upper()', 'o.m({})', 'len({})', 'str({})', 'sorted({})',
       'f({})', 'f(*{})', 'f(**{})', '[*{}]', '{{**{}}}', '{{{}}}', '[q for q in {}]', '[q for q in {} if q]',
       '{{q: q for q in {}}}', 'sum(q for q in {})', '(lambda q: q + {})(1)', '(lambda: {})()',
       '(lambda q=1: (lambda r: q))({})', 'f"{{{}}}"', 'f"{{{}!r:>4}}"', '[q for q in {}] and q',
       '(w := {})', 'not: {}', 'exists: {}', 'structure: {}', 'string:${{{}}}', 'string:a ${{{}}} b',
       'python: {}', '{} | 7', 'econtext.get("{}")']  # fmt: skip


def expressions() -> st.SearchStrategy[str]:
    def extend(inner: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
        return st.one_of(
            st.builds(lambda t, e: t.format(e), st.sampled_from(UNARY + ONE), inner),
            st.builds(lambda t, a, b, c: t.format(a, b, c), st.sampled_from(BINARY), inner, inner, inner),
            # Identity: constants folded and merged template-wide as CPython's compiler does.
            st.builds(lambda a, op, b: f'({a} {op} {b})', inner, st.sampled_from(['is', 'is not']), inner),
        )

    return st.recursive(ATOMS, extend, max_leaves=6)


FUNCTION = re.compile(r'<(function|generator object|coroutine object|bound method) .*? at 0x\?>')


def outcome(engine: Any, expression: str, shadow: bool) -> tuple[str, ...]:
    scope = {**SCOPE, **(SHADOWS if shadow else {})}
    result = evaluate(engine, expression, scope)
    # Functions defined by an expression are named after the enclosing generated code. A repr
    # iterated character by character (`[q for q in f"{repeat}"]`) is joined back first, so
    # its address and module name are normalized like any other.
    return tuple(FUNCTION.sub(r'<\1>', normal(part)) for part in result)


def normal(part: str) -> str:
    if "', '" not in part:
        return part
    # A dictionary of overlapping pairs (`{q: q + n for q in f"{repeat}"}`) doubles each
    # character once its separators are gone: `0xx1199`, `chameleon__rrss..`.
    joined = part.replace("', '", '').replace("': '", '')
    joined = re.sub(r'0x[0-9a-fA-Fx]+', '0x?', joined)
    return joined.replace('chameleon_rs.', 'chameleon.').replace('__rrss', '')


def depends_on_repeat_repr(expression: str, shadow: bool) -> bool:
    """Whether the value is computed from `repeat`'s default repr, which holds its module name
    and a new address in every render (`sorted(str(repeat))`): such values can't be compared
    across engines. Checked on Chameleon with the repr pinned to two different strings."""
    repeat_dict = chameleon.tal.RepeatDict
    results = []
    for text in ('<R>', '<RR 2>'):
        repeat_dict.__repr__ = lambda self, text=text: text
        try:
            results.append(outcome(chameleon, expression, shadow))
        finally:
            del repeat_dict.__repr__
    return results[0] != results[1]


@pytest.mark.filterwarnings('ignore::SyntaxWarning')  # `x is 1.5` and the like, from both engines
@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(expressions(), st.booleans())
def test_generated_expressions(expression: str, shadow: bool) -> None:
    want = outcome(chameleon, expression, shadow)
    got = outcome(chameleon_rs, expression, shadow)
    if want[:2] == ('raise', 'SyntaxError') and got[:2] == ('raise', 'SyntaxError'):
        return  # the message names the generated module
    if got != want and depends_on_repeat_repr(expression, shadow):
        return
    assert got == want, expression
