# Ported from Chameleon's src/chameleon/compiler.py (`COMPILER_INTERNALS_OR_DISALLOWED`,
# `ExpressionEngine`'s configuration, `ExpressionCompiler`, `ExpressionEvaluator`), modified:
# expressions compile to native programs; `ExpressionEngine` holds the configuration but does
# not generate Python code.
"""Expression evaluation outside a template (``ExpressionEvaluator``, for legacy
applications) and the names Chameleon's compiler module exports.

Chameleon's code generator does not exist in chameleon-rs (templates compile to native
programs, D1-D4), so ``ExpressionEngine.parse`` raises ``NotImplementedError``; custom
expression types receive the bridge's engine instead (``chameleon_rs._bridge``, D11).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

from chameleon_rs._native import Program, Scope
from chameleon_rs.exc import RenderError
from chameleon_rs.tales import (
    ExistsExpr,
    ImportExpr,
    NotExpr,
    PythonExpr,
    StringExpr,
    StructureExpr,
    native_types,
)

# Disallowing the use of the following symbols to avoid misunderstandings.
COMPILER_INTERNALS_OR_DISALLOWED = {
    'econtext',
    'rcontext',
}

_DEFAULT_TYPES: Mapping[str, Any] = {
    'python': PythonExpr,
    'string': StringExpr,
    'not': NotExpr,
    'exists': ExistsExpr,
    'import': ImportExpr,
    'structure': StructureExpr,
}


class ExpressionEngine:
    """An expression engine's configuration: the ``parser`` (a
    ``tales.ExpressionParser``, whose ``factories`` and ``default`` say which expression
    types exist), the escape set, the ``default`` value and marker, and ``literal_false``."""

    supported_char_escape_set = frozenset({'&', '<', '>'})

    def __init__(
        self,
        parser: Any,
        char_escape: Any = (),
        default: Any = None,
        default_marker: Any = None,
        literal_false: bool = True,
    ) -> None:
        self._parser = parser
        self._char_escape = char_escape
        self._default = default
        self._default_marker = default_marker
        self._literal_false = literal_false

    def parse(self, string: str, handle_errors: bool = True, char_escape: Any = None) -> ExpressionCompiler:
        raise NotImplementedError(
            'chameleon-rs compiles expressions natively and does not generate Python code; '
            'use ExpressionEvaluator to evaluate an expression'
        )


class ExpressionCompiler:
    def __init__(self, compiler: Callable[..., Any], engine: ExpressionEngine) -> None:
        self.compiler = compiler
        self.engine = engine

    def assign_bool(self, target: Any, s: Any) -> Any:
        return self.compiler(target, self.engine, 'bool', s)

    def assign_text(self, target: Any) -> Any:
        return self.compiler(target, self.engine, 'text')

    def assign_value(self, target: Any) -> Any:
        return self.compiler(target, self.engine)


def _attribute_text(expression: str) -> str:
    """``expression`` as a ``tal:define`` clause in a double-quoted attribute value."""
    return expression.replace(';', ';;').replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')


class ExpressionEvaluator:
    """Evaluates dynamic expression.

    This is not particularly efficient, but supported for legacy applications.

    >>> from chameleon_rs import tales
    >>> parser = tales.ExpressionParser({'python': tales.PythonExpr}, 'python')
    >>> engine = functools.partial(ExpressionEngine, parser)

    >>> evaluator = ExpressionEvaluator(engine, {
    ...     'foo': 'bar',
    ... })

    We'll use the following convenience function to test the expression evaluator.

    >>> from chameleon_rs.utils import Scope
    >>> def evaluate(d, *args):
    ...     return evaluator(Scope(d), *args)

    The evaluation function is passed the local and remote context, the expression type
    and finally the expression.

    >>> evaluate({'boo': 'baz'}, {}, 'python', 'foo + boo')
    'barbaz'

    The cache is now primed:

    >>> evaluate({'boo': 'baz'}, {}, 'python', 'foo + boo')
    'barbaz'

    Note that the call method supports currying of the expression argument:

    >>> python = evaluate({'boo': 'baz'}, {}, 'python')
    >>> python('foo + boo')
    'barbaz'

    The expression is compiled as a native program the first time and evaluated in
    ``econtext`` itself (names it assigns stay there); the value is also assigned to
    ``econtext['_result']``, as Chameleon's evaluation does. Errors are raised as they are
    (not formatted as a render error). ``rcontext`` is not used.
    """

    __slots__ = '_builtins', '_cache', '_engine', '_names'

    def __init__(self, engine: Callable[[], Any], builtins: Mapping[str, Any]) -> None:
        self._engine = engine
        self._names, self._builtins = zip(*builtins.items(), strict=True)
        self._cache: dict[str, Program] = {}

    def __call__(
        self, econtext: MutableMapping[str, Any], rcontext: Any, expression_type: str, string: str | None = None
    ) -> Any:
        if string is None:
            return functools.partial(self.__call__, econtext, rcontext, expression_type)

        expression = f'{expression_type}:{string}'

        program = self._cache.get(expression)
        if program is None:
            program = self._cache[expression] = self._compile(expression)

        out: list[Any] = []

        def sink(value: Any) -> None:
            out.append(value)

        # The caller's scope itself, as Chameleon evaluates in it (a plain mapping, which
        # Chameleon rejects, is copied into a new one).
        variables = econtext if isinstance(econtext, Scope) else Scope(econtext)
        builtin_values = dict(zip(self._names, self._builtins, strict=True))
        builtin_values['__result'] = sink
        try:
            program.render_scope(variables, builtin_values)
        except RenderError as exc:
            original = exc.__context__
            if original is None:
                raise
            raise original from None
        result = out[0]
        econtext['_result'] = result
        return result

    def _compile(self, expression: str) -> Program:
        engine = self._engine()
        parser = getattr(engine, '_parser', None)
        factories = getattr(parser, 'factories', _DEFAULT_TYPES)
        default = getattr(parser, 'default', 'python')
        source = f'<tal:block define="_result {_attribute_text(expression)}" replace="__result(_result)" />'
        return Program(
            source,
            '<string>',
            expression_types=native_types(factories),
            default_expression=default,
            builtins=sorted({*self._names, '__result'}),
        )
