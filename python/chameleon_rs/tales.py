# Ported from Chameleon's src/chameleon/tales.py (the expression type classes PythonExpr,
# StringExpr, NotExpr, ExistsExpr, ImportExpr, StructureExpr, ProxyExpr, ExpressionParser,
# and `match_prefix`), modified: Chameleon's types are compiled natively by chameleon-rs, so
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

match_prefix = re.compile(r'^\s*([a-z][a-z0-9\-_]*):').match


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
