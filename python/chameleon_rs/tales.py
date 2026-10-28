# Ported from Chameleon's src/chameleon/tales.py (the expression type classes, ExpressionParser,
# `match_prefix`, `split_parts`, `re_continuation`, and verbatim `TalesExpr`, `resolve_global`,
# `transform_attribute`, `test`, `SimpleEngine`, `SimpleCompiler`), modified: Chameleon's types
# are compiled natively by chameleon-rs, so these classes name them in a template's
# `expression_types`. Called from Python (a subclass, or a custom type delegating to one),
# PythonExpr, NotExpr, ExistsExpr, ImportExpr and StructureExpr compile as Chameleon's do;
# StringExpr and ProxyExpr delegate to the engine.
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
from chameleon_rs.astutil import Builtin, ItemLookupOnAttributeErrorVisitor, Symbol, parse, store
from chameleon_rs.codegen import TemplateCodeGenerator, reverse_builtin_map, template
from chameleon_rs.exc import ExpressionError
from chameleon_rs.parser import substitute
from chameleon_rs.utils import Markup, lookup_attr, resolve_dotted

split_parts = re.compile(r'(?<!\\)\|')
match_prefix = re.compile(r'^\s*([a-z][a-z0-9\-_]*):').match
re_continuation = re.compile(r'\\\s*$', re.MULTILINE)


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

        body: list[ast.stmt]
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


class _NativeExpr:
    """A type compiled natively that Chameleon also compiles by delegating to the engine.
    Calling an instance (a custom type delegating to it, or a subclass) asks the engine for
    the same expression under this type's usual prefix."""

    prefix = ''

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.expr, engine: Any) -> list[ast.stmt]:
        return engine.parse(f'{self.prefix}:{self.expression}').assign_value(target)  # type: ignore[no-any-return]


# The types below compile natively when a template names them. Their methods are Chameleon's,
# for the code that calls them from Python instead: a subclass (z3c.pt's `PythonExpr` calls
# `translate`, its `ExistsExpr` prefixes `nocall:`) or a custom type that delegates to one.
# Those calls go through the AST bridge.


class PythonExpr(TalesExpr):
    r"""Python expression compiler.

    >>> test(PythonExpr('2 + 2'))
    4

    The Python expression is a TALES expression. That means we can use
    the pipe operator:

    >>> test(PythonExpr('foo | 2 + 2 | 5'))
    4

    To include a pipe character, use a backslash escape sequence:

    >>> test(PythonExpr(r'"\|"'))
    '|'
    """

    transform = ItemLookupOnAttributeErrorVisitor(transform_attribute)

    def parse(self, string):
        return parse(string, 'eval').body  # ty: ignore[unresolved-attribute]

    def translate(self, expression, target):
        # Strip spaces
        string = expression.strip()

        # Convert line continuations to newlines
        string = substitute(re_continuation, '\n', string)

        # Convert newlines to spaces
        string = string.replace('\n', ' ')

        try:
            value = self.parse(string)
        except SyntaxError as exc:
            raise ExpressionError(exc.msg, string) from None

        # Transform attribute lookups to allow fallback to item lookup
        result = self.transform.visit(value)

        return [ast.Assign(targets=[target], value=result)]


class StringExpr(_NativeExpr):
    """Text with ``${expression}`` and ``$name`` substitutions (``$$`` is a dollar)."""

    prefix = 'string'

    def __init__(self, expression: str, braces_required: bool = False) -> None:
        super().__init__(expression)
        self.braces_required = braces_required


class NotExpr:
    """Negates the expression.

    >>> engine = SimpleEngine(PythonExpr)

    >>> test(NotExpr('False'), engine)
    True
    >>> test(NotExpr('True'), engine)
    False
    """

    def __init__(self, expression) -> None:
        self.expression = expression

    def __call__(self, target, engine):
        compiler = engine.parse(self.expression)
        body = compiler.assign_value(target)
        return body + template('target = not target', target=target)


class ExistsExpr:
    """Boolean wrapper.

    Return 0 if the expression results in an exception, otherwise 1.

    As a means to generate exceptions, we set up an expression engine
    which evaluates the provided expression using Python:

    >>> engine = SimpleEngine(PythonExpr)

    >>> test(ExistsExpr('int(0)'), engine)
    1
    >>> test(ExistsExpr('int(None)'), engine)
    0

    """

    exceptions = AttributeError, LookupError, TypeError, NameError

    def __init__(self, expression) -> None:
        self.expression = expression

    def __call__(self, target, engine):
        ignore = store('__ignore')
        compiler = engine.parse(self.expression, False)
        body = compiler.assign_value(ignore)

        classes = list(map(resolve_global, self.exceptions))

        return [
            ast.Try(
                body=body,
                handlers=[
                    ast.ExceptHandler(
                        type=ast.Tuple(elts=classes, ctx=ast.Load()),
                        name=None,
                        body=template('target = 0', target=target),
                    )
                ],
                finalbody=[],
                orelse=template('target = 1', target=target),
            )
        ]


class ImportExpr:
    """The object a dotted name refers to."""

    re_dotted = re.compile(r'^[A-Za-z.]+$')

    def __init__(self, expression) -> None:
        self.expression = expression

    def __call__(self, target, engine):
        string = self.expression.strip().replace('\n', ' ')
        value = template(
            'RESOLVE(NAME)',
            RESOLVE=Symbol(resolve_dotted),
            NAME=ast.Constant(string),
            mode='eval',
        )
        return [ast.Assign(targets=[target], value=value)]


class StructureExpr:
    """Wraps the expression result as 'structure'.

    >>> engine = SimpleEngine(PythonExpr)

    >>> test(StructureExpr('\"<tt>foo</tt>\"'), engine)
    '<tt>foo</tt>'
    """

    wrapper_class = Symbol(Markup)

    def __init__(self, expression) -> None:
        self.expression = expression

    def __call__(self, target, engine):
        compiler = engine.parse(self.expression)
        body = compiler.assign_value(target)
        return body + template(
            'target = wrapper(target)',
            target=target,
            wrapper=self.wrapper_class,
        )


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
    """``(prefix, spec)`` for each entry, as the native compiler reads them, then the Markdown
    profiles ``chameleon_rs.configure(markdown=...)`` set up, for prefixes the template class
    doesn't define itself (a custom ``markdown`` type an application registers wins)."""
    from chameleon_rs import _settings

    specs = [(prefix, _spec(factory)) for prefix, factory in expression_types.items()]
    specs.extend(
        (prefix, ('markdown', profile))
        for prefix, profile in _settings.markdown_profiles.items()
        if prefix not in expression_types
    )
    return specs


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


class SimpleEngine:
    expression = PythonExpr

    def __init__(self, expression=None) -> None:
        if expression is not None:
            self.expression = expression

    def parse(self, string, handle_errors=False, char_escape=None):
        compiler = self.expression(string)
        return SimpleCompiler(compiler, self)


class SimpleCompiler:
    def __init__(self, compiler, engine) -> None:
        self.compiler = compiler
        self.engine = engine

    def assign_text(self, target):
        """Assign expression string as a text value."""

        return self._assign_value_and_coerce(target, 'str')

    def assign_value(self, target):
        """Assign expression string as object value."""

        return self.compiler(target, self.engine)

    def _assign_value_and_coerce(self, target, builtin):
        return self.assign_value(target) + template('target = builtin(target)', target=target, builtin=builtin)


def test(expression, engine=None, **env):
    """Chameleon's doctest helper: compiles and runs ``expression`` (a type instance) with
    Python, as Chameleon's compiler would, and returns the value it assigns."""
    if engine is None:
        engine = SimpleEngine()

    body = expression(store('result'), engine)
    module = ast.Module(body, [])
    module = ast.fix_missing_locations(module)
    env['rcontext'] = {}
    source = TemplateCodeGenerator(module).code
    code = compile(source, '<string>', 'exec')
    exec(code, env)  # noqa: S102 - Chameleon's doctest helper, runs a type's own statements
    result = env['result']

    if isinstance(result, str):
        result = str(result)

    return result


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
    'SimpleCompiler',
    'SimpleEngine',
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
    'test',
    'transform_attribute',
]
