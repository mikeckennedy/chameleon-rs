# Ported from Chameleon's src/chameleon/tales.py (the expression type classes PythonExpr,
# StringExpr, NotExpr, ExistsExpr, ImportExpr, StructureExpr, ProxyExpr, ExpressionParser,
# `match_prefix`, `split_parts`, `re_continuation`, and verbatim `TalesExpr`, `resolve_global`,
# `transform_attribute`), modified: Chameleon's types are compiled natively by chameleon-rs, so
# these classes name them in a template's `expression_types`; called directly (from a custom
# type), they delegate to the engine.
"""TALES expression types.

A template class's ``expression_types`` maps prefixes to types, as in Chameleon::

    class MyTemplate(PageTemplate):
        expression_types = {**PageTemplate.expression_types, 'upper': UpperExpr}

Chameleon's own types (the classes below, and ``functools.partial(ProxyExpr, name, ...)``)
compile natively. Any other entry is a custom type: a factory called with the expression
text, returning a callable ``(target, engine) -> list[ast.stmt]``, which runs through the AST
bridge (``chameleon_rs._bridge``).
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Mapping
from typing import Any

from chameleon_rs._native import DEFAULT_MARKER
from chameleon_rs.astutil import Builtin, Symbol
from chameleon_rs.codegen import reverse_builtin_map, template
from chameleon_rs.exc import ExpressionError
from chameleon_rs.utils import lookup_attr

split_parts = re.compile(r'(?<!\\)\|')
match_prefix = re.compile(r'^\s*([a-z][a-z0-9\-_]*):').match
re_continuation = re.compile(r'\\\s*$', re.MULTILINE)


class _NativeExpr:
    """A type compiled natively. Calling an instance (a custom type delegating to it) asks
    the engine for the same expression under this type's usual prefix."""

    prefix = ''

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.expr, engine: Any) -> list[ast.stmt]:
        return engine.parse(f'{self.prefix}:{self.expression}').assign_value(target)  # type: ignore[no-any-return]


class PythonExpr(_NativeExpr):
    """Python expressions, with TALES pipes (``a | b``; ``\\|`` is a literal pipe)."""

    prefix = 'python'


class StringExpr(_NativeExpr):
    """Text with ``${expression}`` and ``$name`` substitutions (``$$`` is a dollar)."""

    prefix = 'string'

    def __init__(self, expression: str, braces_required: bool = False) -> None:
        super().__init__(expression)
        self.braces_required = braces_required


class NotExpr(_NativeExpr):
    """``not`` of the expression's value."""

    prefix = 'not'


class ExistsExpr(_NativeExpr):
    """``1`` if the expression evaluates, ``0`` if it raises ``AttributeError``,
    ``LookupError``, ``TypeError`` or ``NameError``."""

    prefix = 'exists'


class ImportExpr(_NativeExpr):
    """The object a dotted name refers to."""

    prefix = 'import'


class StructureExpr(_NativeExpr):
    """The value wrapped in ``Markup``, so it renders unescaped."""

    prefix = 'structure'


class ProxyExpr(_NativeExpr):
    """An interpolated string (braces optional) passed to the function bound to ``name``,
    with TALES pipes: ``load:`` is ``partial(ProxyExpr, '__loader', ignore_prefix=False)``."""

    def __init__(self, name: str, expression: str, ignore_prefix: bool = True) -> None:
        super().__init__(expression)
        self.name = name
        self.ignore_prefix = ignore_prefix


_SIMPLE: dict[type, tuple[Any, ...]] = {
    PythonExpr: ('python',),
    NotExpr: ('not',),
    ExistsExpr: ('exists',),
    ImportExpr: ('import',),
    StructureExpr: ('structure',),
}

# Chameleon's own classes (a template class may name them when chameleon is importable).
_CHAMELEON = {'PythonExpr', 'StringExpr', 'NotExpr', 'ExistsExpr', 'ImportExpr', 'StructureExpr', 'ProxyExpr'}


def _ours(cls: Any) -> Any:
    if isinstance(cls, type) and cls.__module__ == 'chameleon.tales' and cls.__qualname__ in _CHAMELEON:
        return globals()[cls.__qualname__]
    return cls


def _spec(factory: Any) -> tuple[Any, ...]:
    cls = _ours(factory)
    if cls in _SIMPLE:
        return _SIMPLE[cls]
    if cls is StringExpr:
        return ('string', False)
    if isinstance(factory, functools.partial):
        func = _ours(factory.func)
        keywords = factory.keywords or {}
        if func is StringExpr and not factory.args and set(keywords) <= {'braces_required'}:
            return ('string', bool(keywords.get('braces_required', False)))
        if func is ProxyExpr and len(factory.args) == 1 and set(keywords) <= {'ignore_prefix'}:
            return ('proxy', str(factory.args[0]), bool(keywords.get('ignore_prefix', True)))
    return ('custom', factory)


def native_types(expression_types: Mapping[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    """``(prefix, spec)`` for each entry, as the native compiler reads them."""
    return [(prefix, _spec(factory)) for prefix, factory in expression_types.items()]


# Chameleon's base class for TALES expression types, vendored for custom types that subclass
# it (z3c.pt does): a subclass's `translate(expression, target)` returns statements for one
# pipe alternative; `__call__` chains the alternatives with `try`/`except`, which the AST
# bridge (D11) compiles.


def resolve_global(value):
    name = reverse_builtin_map.get(value)
    if name is not None:
        return Builtin(name)

    return Symbol(value)


def transform_attribute(node):
    return template(
        'lookup(object, name)', lookup=Symbol(lookup_attr), object=node.value, name=ast.Constant(node.attr), mode='eval'
    )


class TalesExpr:
    """Base class.

    This class helps implementations for the Template Attribute
    Language Expression Syntax (TALES).

    The syntax evaluates one or more expressions, separated by '|'
    (pipe). The first expression that succeeds, is returned.

    Expression:

      expression    := (type ':')? line ('|' expression)?
      line          := .*

    Expression lines may not contain the pipe character unless
    escaped. It has a special meaning:

    If the expression to the left of the pipe fails (raises one of the
    exceptions listed in ``catch_exceptions``), evaluation proceeds to
    the expression(s) on the right.

    Subclasses must implement ``translate`` which assigns a value for
    a given expression.

    >>> class PythonPipeExpr(TalesExpr):
    ...     def translate(self, expression, target):
    ...         compiler = PythonExpr(expression)
    ...         return compiler(target, None)

    >>> test(PythonPipeExpr('foo | bar | 42'))
    42

    >>> test(PythonPipeExpr('foo|42'))
    42
    """

    exceptions = AttributeError, NameError, LookupError, TypeError, ValueError

    ignore_prefix = True

    def __init__(self, expression):
        self.expression = expression

    def __call__(self, target, engine):
        remaining = self.expression
        assignments = []

        while remaining:
            if self.ignore_prefix and match_prefix(remaining) is not None:
                compiler = engine.parse(remaining)
                assignment = compiler.assign_value(target)
                remaining = ''
            else:
                for m in split_parts.finditer(remaining):
                    expression = remaining[: m.start()]
                    remaining = remaining[m.end() :]
                    break
                else:
                    expression = remaining
                    remaining = ''

                expression = expression.replace('\\|', '|')
                assignment = self.translate_proxy(engine, expression, target)
            assignments.append(assignment)

        if not assignments:
            if not remaining:
                raise ExpressionError('No input:', remaining)

            assignments.append(self.translate_proxy(engine, remaining, target))

        for i, assignment in enumerate(reversed(assignments)):
            if i == 0:
                body = assignment
            else:
                body = [
                    ast.Try(
                        body=assignment,
                        handlers=[
                            ast.ExceptHandler(
                                type=ast.Tuple(elts=list(map(resolve_global, self.exceptions)), ctx=ast.Load()),
                                name=None,
                                body=body,
                            )
                        ],
                        finalbody=[],
                        orelse=[],
                    )
                ]

        return body

    def translate_proxy(self, engine, *args):
        """Default implementation delegates to ``translate`` method."""

        return self.translate(*args)

    def translate(self, expression, target):
        """Return statements that assign a value to ``target``."""

        raise NotImplementedError('Must be implemented by a subclass.')


class ExpressionParser:
    """Chameleon's interface: ``factories`` by prefix, and the default type."""

    def __init__(self, factories: Mapping[str, Any], default: str) -> None:
        self.factories = factories
        self.default = default

    def __call__(self, expression: str) -> Any:
        m = match_prefix(expression)
        if m is not None:
            prefix = m.group(1)
            expression = expression[m.end() :]
        else:
            prefix = self.default

        try:
            factory = self.factories[prefix]
        except KeyError as exc:
            raise LookupError(f'Unknown expression type: {exc!s}.') from None

        return factory(expression)


__all__ = [
    'DEFAULT_MARKER',
    'Builtin',
    'ExistsExpr',
    'ExpressionError',
    'ExpressionParser',
    'ImportExpr',
    'NotExpr',
    'ProxyExpr',
    'PythonExpr',
    'StringExpr',
    'StructureExpr',
    'Symbol',
    'TalesExpr',
    'match_prefix',
    'native_types',
    're_continuation',
    'resolve_global',
    'split_parts',
    'template',
    'transform_attribute',
]
