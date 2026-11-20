"""Random `<?python ?>` blocks and Python expressions for differential tests: seeded, so a
failing case is its seed.

The shapes are the ones the name rewriting (`chameleon_rs._rewrite`) treats specially and
Chameleon renders: assignments, `def` (nested, with parameters and type parameters), lambdas,
comprehensions, imports, loops, `try`, attribute stores, the walrus, and reads of scope names,
builtins and template builtins at every depth. Left out, as chameleon-rs differs there on
purpose (user_guide/compatibility.qmd): `class`, `match`, `except ... as`, and binding a
builtin's or a template builtin's name in a block.
"""

from __future__ import annotations

import random
import re
from typing import Any

# Names a block or a lambda may bind, and what an expression may read.
BOUND = ['a', 'b', 'x', 'q', 'f', 'g', 'os', 'd', 'missing']
READ = [*BOUND, 'nothing', 'template', 'len', 'str', 'default', 'repeat']


def expression(pick: random.Random, depth: int = 0) -> str:
    kind = pick.randrange(14 if depth < 3 else 5)
    name = pick.choice(READ)

    def inner() -> str:
        return expression(pick, depth + 1)

    if kind == 0:
        return name
    if kind == 1:
        return str(pick.randrange(3))
    if kind == 2:
        return f'{name}.real'
    if kind == 3:
        return f'{name} is None'
    if kind == 4:
        return f'"{name}"'
    if kind == 5:
        return f'(lambda {pick.choice(BOUND)}: {inner()})({inner()})'
    if kind == 6:
        return f'(lambda: {inner()})()'
    if kind == 7:
        return f'[{inner()} for {pick.choice(BOUND)} in [1, 2]]'
    if kind == 8:
        return f'{name}({inner()})'
    if kind == 9:
        return f'({inner()} or {inner()})'
    if kind == 10:
        return f'{{"k": {inner()}}}'
    if kind == 11:
        return f'{name}.k'
    if kind == 12:
        return f'({pick.choice(BOUND)} := {inner()})'
    return f'({inner()}, {inner()})'


def statement(pick: random.Random) -> str:
    kind = pick.randrange(12)
    name = pick.choice(BOUND)

    def other() -> str:
        return pick.choice(BOUND)

    def value() -> str:
        return expression(pick)

    if kind == 0:
        return f'{name} = {value()}'
    if kind == 1:
        return f'def {name}({other()}):\n    {other()} = {value()}\n    return {value()}'
    if kind == 2:
        return f'import os.path as {name}'
    if kind == 3:
        return f'from os import path as {name}'
    if kind == 4:
        return f'for {name} in [1, 2]:\n    {other()} = {value()}'
    if kind == 5:
        return f'{name} = lambda {other()}: {value()}'
    if kind == 6:
        return f'if {value()}:\n    {other()} = {value()}'
    if kind == 7:
        return f'try:\n    {other()} = {value()}\nexcept Exception:\n    {other()} = 0'
    if kind == 8:
        return f'{name}.attr = {value()}'
    if kind == 9:
        return f'def {name}():\n    def inner({other()}):\n        return {value()}\n    return inner({value()})'
    if kind == 10:
        return f'def {name}[T: {pick.choice(READ)}]({other()}: T) -> T:\n    return {value()}'
    return f'{name} += 1'


def template(seed: int) -> str:
    """The template of ``seed``: up to three statements in a block, then up to three values."""
    pick = random.Random(seed)
    block = '\n'.join(statement(pick) for _ in range(pick.randint(0, 3)))
    values = '|'.join('${' + expression(pick) + '}' for _ in range(pick.randint(1, 3)))
    return (f'<?python\n{block}\n?>' if block else '') + values


class Thing:
    k = 'K'

    def __call__(self, *args: Any) -> Any:
        return args[0] if args else self


def outcome(engine: Any, source: str) -> str:
    """What ``engine`` renders ``source`` to, or its error, without what differs by design."""
    try:
        out = engine.PageTemplate(source)(a=1, b=None, x=Thing(), d={'k': 5})
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        out = f'{type(e).__name__}: {str(e).splitlines()[0]}'
    if out.startswith(('NameError', 'UnboundLocalError')):
        # A name of a block that is not bound: in places a local of Chameleon's render function
        # (an `UnboundLocalError`, which is a `NameError`), here a name of the block's namespace.
        return 'NameError'
    if out.startswith('SyntaxError'):
        # Each engine's compiler finds its first error in its own order.
        return 'SyntaxError'
    # Addresses, where a function defined in a block or an expression lives in Chameleon's
    # generated module, and Python's spelling suggestions (the template classes' attributes differ).
    out = re.sub(r'0x[0-9a-f]+', '', out).replace('chameleon_rs.', 'chameleon.')
    for prefix in ('initialize.<locals>.render.<locals>.', 'initialize.&lt;locals&gt;.render.&lt;locals&gt;.'):
        out = out.replace(prefix, '')
    return out.split('. Did you mean')[0]
