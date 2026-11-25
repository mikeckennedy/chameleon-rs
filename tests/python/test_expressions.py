"""Phase 5, expressions: every TALES expression type and pipe, name resolution, non-strict
mode, custom expression types, `load:`, and free variables, compared with the reference
Chameleon (same value or same exception type and arguments)."""

from __future__ import annotations

import ast
import functools
import itertools
import os
import warnings
from pathlib import Path
from typing import Any

import chameleon
import chameleon.astutil
import chameleon.exc
import chameleon.tales
import chameleon_rs
import chameleon_rs.tales
import pytest
from chameleon_rs._native import _testing
from chameleon_rs._rewrite import Rewriter
from exprtest import evaluate, render


class O:
    def __str__(self) -> str:
        return 'O!'


class Html:
    def __html__(self) -> str:
        return '<i>html</i>'


class GetItem:
    def __getitem__(self, key: str) -> str:
        if key == 'k':
            return 'item'
        raise KeyError(key)


class Methods:
    """Method calls: plain, raising `AttributeError` from inside, a property returning a
    callable, a property whose getter raises `AttributeError` (then `__getitem__`)."""

    def m(self, *args: Any) -> str:
        return f'm{args}'

    def fails(self) -> str:
        return self.missing  # ty: ignore[unresolved-attribute] - raises AttributeError on purpose

    @property
    def prop(self) -> Any:
        return lambda: 'prop called'

    @property
    def broken(self) -> Any:
        raise AttributeError('broken')

    def __getitem__(self, key: str) -> Any:
        if key == 'broken':
            return lambda: 'item for broken'
        raise KeyError(key)


# (expression, scope): the value (or error) must match.
VALUES: list[tuple[str, dict[str, Any]]] = [
    # string:
    ('string: hello ${x} $y $$ $${z}', {'x': 1, 'y': '<'}),
    ('string:${x}', {'x': None}),
    ('string:${x}', {'x': 5}),
    ('string:${x}', {'x': 1.5}),
    ('string:${x}', {'x': O()}),
    ('string:${x}', {'x': Html()}),
    ('string:${x}', {'x': b'ab'}),
    ('string:${x}', {'x': True}),
    ('string:${x}', {'x': [1]}),
    ('string:a${x}b', {'x': None}),
    ('string:${x}${x}', {'x': None}),
    ('string:', {}),
    ('string:${}', {}),
    ('string:$', {}),
    ('string:$1', {}),
    ('string:${default}', {}),
    ('string:${x | 3}', {}),
    ('string:${python: 1+1}', {}),
    ('string:${string: in}', {}),
    ('string:${structure: x}', {'x': '<'}),
    ('string: a | b', {}),
    ('string:$x.y', {'x': 1}),
    ('string:$x_1$y', {'x_1': 1, 'y': 2}),
    ('string:$é', {}),
    ('string:${1 + $b}', {'b': 'B'}),
    ('string:${a} ${b', {'a': 1}),
    ('string:$$x $$$x $$$$x', {'x': 1}),
    ('string:${x', {'x': 1}),
    ('string:${1 +}', {}),
    ('string:a ${1 +} b', {}),
    ('string:${a}${b}', {'a': [1], 'b': None}),
    ('string:\\${x}', {'x': 1}),
    ('string:${x}}', {'x': 1}),
    ('string:${"}"}', {}),
    ('string:${x} ${"}"}', {'x': 1}),
    ('string:$Python ${x}$', {'Python': 'P', 'x': 2}),
    ('string:${x \\\n + 1}', {'x': 1}),
    ('string:${missing}', {}),
    # not:, exists:, structure:, import:
    ('not: x', {'x': 0}),
    ('not: x', {'x': [1]}),
    ('not: exists: x', {}),
    ('not: x | 1', {}),
    ('not: string:', {}),
    ('not:x|y', {'x': 0}),
    ('not:', {}),
    ('exists: x', {}),
    ('exists: x', {'x': 0}),
    ('exists: int("a")', {}),
    ('exists: x.y', {'x': 1}),
    ('exists: x | y', {}),
    ('exists: nothing.x', {}),
    ('exists: [][1]', {}),
    ('exists: {}["a"]', {}),
    ('exists: len(1)', {}),
    ('exists:x|y', {'y': 0}),
    ('exists:', {}),
    ('exists: import: nonexistent_module_zz', {}),
    ('structure: x', {'x': '<b>'}),
    ('structure: 1', {}),
    ('structure: None', {}),
    ('structure: string:${x}', {'x': '<'}),
    ('import: os.path', {}),
    ('import: os.path.join', {}),
    ('import:  os.\npath', {}),
    ('import: ', {}),
    ('import: nonexistent_module_zz', {}),
    ('import: .relative', {}),
    # Pipes.
    ('python: 1 | 2', {}),
    ('x | python: 4', {}),
    ('x | y', {}),
    ('x | not: y', {'y': 0}),
    ('x | string:a', {}),
    ('x | import: nonexistent_module_zz', {}),
    ('x | nothing', {}),
    ('x |', {}),
    ('| x', {'x': 1}),
    ('x || y', {'y': 2}),
    ('a \\| b', {'a': 1, 'b': 2}),
    ('"a\\|b"', {}),
    ('x.y | 1', {'x': 1}),
    ('x["k"] | 1', {'x': {}}),
    ('x[5] | 1', {'x': []}),
    ('int("a") | 1', {}),
    ('len(1) | 1', {}),
    ('1 / 0 | 1', {}),
    ('x.k | 1', {'x': GetItem()}),
    ('x.z | 1', {'x': GetItem()}),
    ('[1].y | 2', {}),
    ('missing | [x for x in [1]]', {}),
    ('python:', {}),
    ('foo: bar', {}),
    ('Python: 1', {}),
    ('lambda: 1', {}),
    # Names.
    ('nothing', {}),
    ('nothing', {'nothing': 5}),
    ('template is not None', {}),
    ('macros is not None', {}),
    ('len', {'len': 3}),
    ('len("ab")', {}),
    ('translate("a")', {}),
    ('decode(b"a")', {}),
    ('on_error_handler', {}),
    ('target_language', {}),
    ('econtext.get("x")', {'x': 1}),
    ('rcontext', {}),
    ('__default', {}),
    ('__builtins__ is not None', {}),
    ('__rs_tpl', {}),
    ('[__rs_default]', {}),
    ('__lookup_attr', {}),
    ('type(__builtins__)', {}),
    ('repeat is not None', {}),
    ('default', {}),
    ('attrs', {}),
    # The fast path and the fallback.
    ('x[1:]', {'x': [1, 2, 3]}),
    ('x[::2]', {'x': [1, 2, 3]}),
    ('x[:-1]', {'x': 'abc'}),
    ('x[1:2, 3]', {'x': {(slice(1, 2), 3): 'k'}}),
    ('x[...]', {'x': {Ellipsis: 'e'}}),
    ('[translate for translate in [1, 2]]', {}),
    ('(lambda a=x: a)()', {'x': 4}),
    ('(lambda a: (lambda b: a + b))(1)(2)', {}),
    ('[q for q in [1, 2]]', {}),
    ('{k: v for k, v in [(1, 2)]}', {}),
    ('f"{x!r:>5}"', {'x': 'a'}),
    ('f(*[1], **{"b": 2})', {'f': lambda *a, **k: (a, k)}),
    ('1 < x < 3 < y', {'x': 2, 'y': 1}),
    ('x if y else z', {'x': 1, 'y': 0, 'z': 2}),
    ('x and y or z', {'x': 1, 'y': 0, 'z': 'z'}),
    ('not x', {'x': ''}),
    ('-x ** 2', {'x': 3}),
    ('x @ y', {'x': 1, 'y': 2}),
    ('{**x}', {'x': {'a': 1}}),
    ('[*x]', {'x': 'ab'}),
    ('"{:,}".format(x)', {'x': 1234567}),
    ('"x".nope', {}),
    ('x.__class__.__name__', {'x': 1}),
    ('x \\\n + 1', {'x': 1}),
    ('x +\n 1', {'x': 1}),
    ('await x', {'x': 1}),
    # Phase 10: method calls with constant arguments go through `PyObject_VectorcallMethod`.
    ('x.upper()', {'x': 'ab'}),
    ('x.split(",", 1)', {'x': 'a,b,c'}),
    ('x.get("k")', {'x': {'k': 1}}),
    ('x.nope()', {'x': 'ab'}),
    ('x.k()', {'x': {'k': lambda: 'from item'}}),
    ('x.k(1)', {'x': {'k': lambda v: v + 1}}),
    ('x.k()', {'x': GetItem()}),
    ('x.m()', {'x': Methods()}),
    ('x.fails()', {'x': Methods()}),
    ('x.prop()', {'x': Methods()}),
    ('x.broken()', {'x': Methods()}),
    ('x.m(1, 2)', {'x': Methods()}),
    ('x.m(None)', {'x': Methods()}),
]


@pytest.mark.parametrize(('expression', 'scope'), VALUES, ids=[repr(e) for e, _ in VALUES])
def test_same_value(expression: str, scope: dict[str, Any]) -> None:
    want = evaluate(chameleon, expression, scope)
    got = evaluate(chameleon_rs, expression, scope)
    assert got == want


def test_walrus_target_is_a_syntax_error_as_in_chameleon() -> None:
    # Chameleon's generated module fails to compile (the target became `econtext['y']`).
    for source in ('<p>${(y := 1)}</p>', '<p tal:define="z [y := 2]">${y}</p>'):
        with pytest.raises(SyntaxError) as want:
            chameleon.PageTemplate(source)
        with pytest.raises(SyntaxError) as got:
            chameleon_rs.PageTemplate(source)
        assert got.value.msg == want.value.msg == 'cannot use assignment expressions with subscript'


# Every expression type and pipe combination, in every kind of site.
SITES = [
    '<p>${{{e}}}</p>',
    '<p a="${{{e}}}" b="x${{{e}}}y">t</p>',
    "<p a='${{{e}}}'>t</p>",
    '<p tal:content="{e}">d</p>',
    '<p tal:replace="{e}">d</p>',
    '<p tal:content="structure {e}">d</p>',
    '<p tal:attributes="title {e}" title="T">d</p>',
    # `checked=""`: valueless, Chameleon writes `checkedchecked` (malthe/chameleon#429, tested in
    # test_builder).
    '<input tal:attributes="checked {e}" checked=""/>',
    '<input checked="${{{e}}}"/>',
    '<p tal:condition="{e}">yes</p>',
    '<p tal:define="v {e}">${{v}}</p>',
    '<p tal:repeat="i {e}">${{i}}</p>',
    '<p tal:switch="{e}"><b tal:case="x">X</b><b tal:case="default">D</b></p>',
    '<![CDATA[${{{e}}}]]>',
    '<!-- ${{{e}}} -->',
]

EXPRESSIONS = [
    'x',
    'missing',
    'python: x',
    'string:a${x}b',
    'string:${x}',
    'string:$x',
    'string:${default}',
    'string:${missing | x}',
    'not: x',
    'exists: x',
    'exists: missing',
    'structure: x',
    'import: os.sep',
    'nothing',
    'default',
    'missing | x',
    'missing | string:fb ${x}',
    'missing | not: x',
    'missing | exists: x',
    'missing | structure: x',
    'missing | nothing',
    'missing | default',
    'x | missing',
    'missing.attr | 1',
    'python: missing | 2',
    'not: missing | x',
    'structure: missing | x',
]

SCOPES = [{'x': '<b>&"\''}, {'x': 0}, {'x': 1}, {'x': None}, {'x': ''}, {'x': [1, '<']}, {'x': Html()}]


def _attribute(e: str) -> str:
    return e.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')


@pytest.mark.parametrize('site', SITES)
def test_expression_types_in_every_site(site: str) -> None:
    differences = []
    for expression, scope in itertools.product(EXPRESSIONS, SCOPES):
        source = site.format(e=_attribute(expression) if '"' in site.split('{e}')[0][-12:] else expression)
        want = render(chameleon, source, scope)
        got = render(chameleon_rs, source, scope)
        if got != want:
            differences.append((expression, scope, want, got))
    assert not differences, differences[:5]


# Non-strict mode: a syntax error is raised when its site is reached, with Chameleon's token,
# offset and location.
NON_STRICT = [
    '<p>a</p>\n<p tal:content="1 +">x</p>',
    '<p>${a} ${1 +}</p>',
    "<tal:block replace='bad /// ' />",
    '<p tal:attributes="a 1 +">x</p>',
    '<p a="${1 +}">x</p>',
    '<p tal:content="string:${1 +}">x</p>',
    '<p tal:content="exists: 1 +">x</p>',
    '<p tal:define="x 1 +">x</p>',
    '<p tal:condition="1 +">x</p>',
    '<p tal:repeat="i 1 +">x</p>',
    '<p tal:content="x | 1 +">x</p>',
    '<p tal:content="not:">x</p>',
    '<p>é\nü ${ 1 + }</p>',
    '<p tal:condition="False" tal:content="1 +">x</p>ok',
    '<p tal:condition="False">${1 +}</p>ok',
    '<p tal:switch="1 +"><b tal:case="1">x</b></p>',
]


def _non_strict(engine: Any, source: str) -> Any:
    try:
        return engine.PageTemplate(source, strict=False)()
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        # A TemplateError of either engine.
        return (type(e).__name__, e.offset, str(e.token), tuple(e.location), e.args[0])  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize('source', NON_STRICT)
def test_non_strict_errors_at_render_time(source: str) -> None:
    want = _non_strict(chameleon, source)
    got = _non_strict(chameleon_rs, source)
    assert got == want


def test_non_strict_error_is_raised_fresh_each_render() -> None:
    template = chameleon_rs.PageTemplate('<p tal:content="1 +">x</p>', strict=False)
    errors = []
    for _ in range(2):
        with pytest.raises(chameleon_rs.exc.ExpressionError) as e:
            template()
        errors.append(e.value)
    assert errors[0] is not errors[1]
    assert errors[0].args[0] == 'invalid syntax'


# The doctests of tales.py and compiler.py that concern expressions, as templates.
DOCTESTS = [
    # TalesExpr
    ('foo | bar | 42', {}, 42),
    ('foo|42', {}, 42),
    # PythonExpr
    ('2 + 2', {}, 4),
    ('foo | 2 + 2 | 5', {}, 4),
    ('"\\|"', {}, '|'),
    # NotExpr, StructureExpr, IdentityExpr
    ('not: False', {}, True),
    ('not: True', {}, False),
    ('structure: "<tt>foo</tt>"', {}, '<tt>foo</tt>'),
    ('42', {}, 42),
    # StringExpr
    ('string:Hello ${name}!', {'name': 'world'}, 'Hello world!'),
    ('string:Hello $name!', {'name': 'world'}, 'Hello world!'),
    ('string:$${name}', {}, '${name}'),
    ('string:\\\\${name}', {'name': 'Hello world!'}, '\\\\Hello world!'),
    ("string:Hello ${'a'}${'b'}${'c'}!", {}, 'Hello abc!'),
    ('string:test ${1}${2}', {}, 'test 12'),
    ('string:test $${1}${2}', {}, 'test ${1}2'),
    ('string:test $$', {}, 'test $'),
    ('string:$$.ajax(...)', {}, '$.ajax(...)'),
    ('string:test $$ ${1}', {}, 'test $ 1'),
    # ExistsExpr
    ('exists: int(0)', {}, 1),
    ('exists: int(None)', {}, 0),
    # ExpressionEngine
    ('not False', {}, True),
    ('not: exists: int(None)', {}, True),
    ('not: exists: help', {}, False),
]


@pytest.mark.parametrize(('expression', 'scope', 'result'), DOCTESTS, ids=[d[0] for d in DOCTESTS])
def test_doctests(expression: str, scope: dict[str, Any], result: Any) -> None:
    want = evaluate(chameleon, expression, scope)
    got = evaluate(chameleon_rs, expression, scope)
    assert got == want
    assert got[0] == 'value'
    assert got[2] == repr(result) or got[1] == 'Markup'


def test_doctest_string_javascript() -> None:
    source = """
function($$, oid) {
    $('#' + oid).autocomplete({source: ${'source'}});
}
"""
    body = f'<tal:a define="v string:{source}"><tal:b define="_ sink(v)"/></tal:a>'.replace(';', ';;')
    results = []
    for engine in (chameleon, chameleon_rs):
        out: list[str] = []
        engine.PageTemplate(body)(sink=lambda v, out=out: out.append(v) or '')
        results.append(out[0])
    assert results[0] == results[1]
    assert 'source: source' in results[1]
    assert 'function($, oid)' in results[1]


def test_doctest_string_braces_required() -> None:
    def template_class(engine: Any) -> Any:
        base = engine.PageTemplate
        types = {**base.expression_types, 'string': functools.partial(engine.tales.StringExpr, braces_required=True)}
        return type('Braces', (base,), {'expression_types': types})

    for expression in ('string:Hello $name!', 'string:Hello ${name}!'):
        want = evaluate(chameleon, expression, {'name': 'w'}, template_class(chameleon))
        got = evaluate(chameleon_rs, expression, {'name': 'w'}, template_class(chameleon_rs))
        assert got == want


def test_doctest_name_transform() -> None:
    # NameTransform's doctest, on our rewriter: builtins are looked up in the scope first,
    # `__` names and compiler internals are left alone, aliases are substituted.
    def rewrite(name: str) -> str:
        rewriter = Rewriter({'boo': 'boz'}, template_builtins={'foo', 'bar'})
        return ast.unparse(rewriter.visit(ast.Name(id=name, ctx=ast.Load())))

    assert rewrite('frobnitz') == "econtext.get_name('frobnitz')"
    assert rewrite('foo') == "econtext.get('foo', __rs_tpl['foo'])"
    assert rewrite('len') == "econtext.get('len', len)"
    assert rewrite('__internal') == '__internal'
    assert rewrite('econtext') == 'econtext'
    assert rewrite('boo') == 'boz'


# Custom expression types (the bridge, D11): the same classes registered on both engines.
class UpperExpr:
    """`engine.parse(...).assign_value` and a method call on the target."""

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.Name, engine: Any) -> list[ast.stmt]:
        load = ast.Name(id=target.id, ctx=ast.Load())
        upper = ast.Call(func=ast.Attribute(value=load, attr='upper', ctx=ast.Load()), args=[], keywords=[])
        return [*engine.parse(self.expression).assign_value(target), ast.Assign(targets=[target], value=upper)]


class LenExpr:
    """The StringExpr doctest's engine: the length of the expression text."""

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.Name, engine: Any) -> list[ast.stmt]:
        return [ast.Assign(targets=[target], value=ast.Constant(len(self.expression)))]


class NameExpr:
    """A name in the scope, through Chameleon's name rewriting."""

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.Name, engine: Any) -> list[ast.stmt]:
        return [ast.Assign(targets=[target], value=ast.Name(id=self.expression.strip(), ctx=ast.Load()))]


class TextExpr:
    """`assign_text`: converted (and escaped) like the site converts text."""

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.Name, engine: Any) -> list[ast.stmt]:
        return engine.parse(self.expression).assign_text(target)


class BoolExpr:
    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.Name, engine: Any) -> list[ast.stmt]:
        return engine.parse(self.expression).assign_bool(target, 'yes')


class StoreExpr:
    """Assigns a name: it lands in the template scope."""

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.Name, engine: Any) -> list[ast.stmt]:
        stored = ast.Assign(targets=[ast.Name(id='stored', ctx=ast.Store())], value=ast.Constant(self.expression))
        return [stored, ast.Assign(targets=[target], value=ast.Name(id='stored', ctx=ast.Load()))]


class SymbolExpr:
    """Chameleon's `Symbol` node (an object referenced from the generated code)."""

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __call__(self, target: ast.Name, engine: Any) -> list[ast.stmt]:
        func = chameleon.astutil.Symbol(os.path.basename)
        # Chameleon's own AST node, which its code generator accepts as an expression.
        call = ast.Call(func=func, args=[ast.Constant(self.expression)], keywords=[])  # ty: ignore[invalid-argument-type]
        return [ast.Assign(targets=[target], value=call)]


class BraceExpr:
    """Raises ExpressionError for text with `}`, which drives the Interpolator's retry."""

    error: type[Exception] = Exception

    def __init__(self, expression: str) -> None:
        if '}' in expression:
            raise self.error('no braces', expression)
        self.expression = expression

    def __call__(self, target: ast.Name, engine: Any) -> list[ast.stmt]:
        return [ast.Assign(targets=[target], value=ast.Constant(self.expression.strip()))]


def custom_class(engine: Any, default: str = 'python') -> Any:
    brace = type('Brace', (BraceExpr,), {'error': engine.exc.ExpressionError})
    types = {
        **engine.PageTemplate.expression_types,
        'upper': UpperExpr,
        'len': LenExpr,
        'name': NameExpr,
        'text': TextExpr,
        'bool': BoolExpr,
        'store': StoreExpr,
        'symbol': SymbolExpr,
        'brace': brace,
    }
    return type('Custom', (engine.PageTemplate,), {'expression_types': types, 'default_expression': default})


CUSTOM_RENDERS = [
    ('<p>${upper: x}</p>', {'x': 'abc'}),
    ('<p>${upper: string:a${x}}</p>', {'x': 'b'}),
    ('<p>${upper: missing | x}</p>', {'x': 'q'}),
    ('<p>${missing | upper: x}</p>', {'x': 'q'}),
    ('<p>${upper: x}</p>', {}),
    ('<p>${not: upper: x}</p>', {'x': ''}),
    ('<p>${string:${upper: x}!}</p>', {'x': 'z'}),
    ('<p>${len: hello world}</p>', {}),
    ('<p>${name: x}</p>', {'x': '<i>'}),
    ('<p>${name: x}</p>', {}),
    ('<p>${name: len}</p>', {}),
    ('<p>${text: x}|${text: y}</p>', {'x': '<&>', 'y': None}),
    ('<p a="${text: x}">t</p>', {'x': '<"&>'}),
    ('<p tal:content="text: x">t</p>', {'x': '<"&>'}),
    ('<p tal:attributes="a text: x" a="A">t</p>', {'x': '<"&>'}),
    ('<p tal:attributes="a text: default" a="A">t</p>', {}),
    ('<input tal:attributes="checked bool: x"/>', {'x': 1}),
    ('<p tal:content="bool: x">t</p>', {'x': 0}),
    ('<p tal:define="v store: s">${v} ${stored}</p>', {}),
    ('<p>${symbol: a/b/cd}</p>', {}),
    ('<p>${brace: x} y}</p>', {}),
    ('<p tal:content="exists: name: missing">t</p>', {}),
    # A sub-expression reads what a code block defined (a `def`, an import), in a macro too.
    ("<p><?python\ndef f(a):\n    return a * 2\n?>${upper: f('ab')}</p>", {}),
    ('<p><?python import os.path as osp ?>${upper: osp.basename(x)}</p>', {'x': 'a/b'}),
    (
        '<p metal:define-macro="m"><?python\ndef f(a):\n    return a * 2\n?>${upper: f(x)}</p><i metal:use-macro="m"/>',
        {'x': 'ab'},
    ),
]


@pytest.mark.parametrize(('source', 'scope'), CUSTOM_RENDERS)
def test_custom_expression_types(source: str, scope: dict[str, Any]) -> None:
    want = render(chameleon, source, scope, custom_class(chameleon))
    got = render(chameleon_rs, source, scope, custom_class(chameleon_rs))
    assert got == want


def test_custom_type_as_the_default_expression_type() -> None:
    # The StringExpr doctest with its character-counting engine.
    source = '<p tal:content="string:There are ${hello world} characters in &quot;hello world&quot;">x</p>'
    want = render(chameleon, source, {}, custom_class(chameleon, default='len'))
    got = render(chameleon_rs, source, {}, custom_class(chameleon_rs, default='len'))
    assert got == want == ('ok', '<p>There are 11 characters in "hello world"</p>')


def test_custom_type_compile_error() -> None:
    for engine in (chameleon, chameleon_rs):
        with pytest.raises(engine.exc.ExpressionError):
            custom_class(engine)('<p tal:content="brace: }">x</p>')


def test_custom_type_raising_chameleons_expression_error() -> None:
    # Types written for Chameleon (z3c.pt, Zope's MappedExpr) reject a too-long `${...}`
    # candidate with Chameleon's ExpressionError; the Interpolator retries on it (#8).
    brace = type('Brace', (BraceExpr,), {'error': chameleon.exc.ExpressionError})
    types = {**chameleon_rs.PageTemplate.expression_types, 'brace': brace}
    template = chameleon_rs.PageTemplate(
        '<p class="a-${x} b-${y}">${x} y}</p>', expression_types=types, default_expression='brace'
    )
    assert template() == '<p class="a-x b-y">x y}</p>'


# `load:` (file templates): relative paths, interpolated specs, pipes, packages.
def test_load_expression(tmp_path: Path) -> None:
    (tmp_path / 'other.pt').write_text('<b>${x}</b>')
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / 'deep.pt').write_text('<i tal:define="t load: ../other.pt">${structure: t(x=2)}</i>')
    cases = [
        '<p tal:define="t load: other.pt" tal:content="structure t(x=1)">d</p>',
        '<p tal:define="t load: ${name}.pt" tal:content="structure t(x=1)">d</p>',
        '<p tal:define="t load: $name.pt" tal:content="structure t(x=1)">d</p>',
        '<p tal:define="t load: missing.pt | other.pt" tal:content="structure t(x=1)">d</p>',
        '<p tal:define="t load: missing.pt" tal:content="structure t(x=1)">d</p>',
        '<p tal:define="t load: sub/deep.pt" tal:content="structure t()">d</p>',
        f'<p tal:define="t load: {tmp_path / "other.pt"}" tal:content="structure t(x=3)">d</p>',
        '<p tal:define="t load: chameleon:tests/inputs/hello_world.pt" tal:content="structure t()">d</p>',
        '<p tal:content="python: load(\'x\')">d</p>',
        '<p tal:content="python: __loader is not None">d</p>',
        '<p tal:define="a load: other.pt; b load: other.pt" tal:content="a is b">d</p>',
    ]
    for i, case in enumerate(cases):
        path = tmp_path / f'main{i}.pt'
        path.write_text(case)
        results = [
            render(engine, str(path), {'name': 'other'}, engine.PageTemplateFile)
            for engine in (chameleon, chameleon_rs)
        ]
        assert results[1] == results[0], case


def test_load_is_unknown_in_string_templates() -> None:
    with pytest.raises(chameleon_rs.exc.ExpressionError, match="Unknown expression type: 'load'"):
        chameleon_rs.PageTemplate('<p tal:content="load: x.pt">d</p>')


def test_file_template_reloads(tmp_path: Path) -> None:
    path = tmp_path / 't.pt'
    path.write_text('<p>${x}</p>')
    template = chameleon_rs.PageTemplateFile(path, auto_reload=True)
    assert template(x=1) == '<p>1</p>'
    path.write_text('<b>${x}</b>')
    os.utime(path, (1, 1))
    assert template(x=1) == '<b>1</b>'


# Template builtins.
def test_extra_builtins_and_overrides() -> None:
    for engine in (chameleon, chameleon_rs):
        cls = type('T', (engine.PageTemplate,), {'extra_builtins': {'answer': 42, 'len': lambda x: 'mine'}})
        assert cls('<p>${answer} ${len("abc")} ${nothing}</p>')() == '<p>42 mine </p>'
        assert cls('<p>${answer}</p>')(answer=7) == '<p>7</p>'
        assert cls('<p>${[answer for _ in [1]]}</p>')() == '<p>[42]</p>'


def test_import_is_cached_and_resolved_at_render_time() -> None:
    template = chameleon_rs.PageTemplate('<p tal:define="m import: os.path" tal:content="m is p">x</p>')
    assert template(p=os.path) == '<p>True</p>'


# Free variables (D9), as the resolve table records them per expression site.
FREE = [
    ('<p>${a.b + len(c)}</p>', [('${a.b + len(c)}', ['a', 'c', 'len'], [])]),
    ('<p tal:content="[x for x in y]">d</p>', [('[x for x in y]', ['x', 'y'], ['x'])]),
    ('<p tal:content="econtext[\'a\']">d</p>', [("econtext['a']", None, None)]),
    ('<p tal:content="rcontext">d</p>', [('rcontext', None, None)]),
    (
        '<p tal:content="string:${a} $b">d</p>',
        [('string:${a} $b', ['__decode', '__translate', 'a', 'b', 'target_language'], [])],
    ),
    ('<p tal:content="nothing">d</p>', [('nothing', ['nothing'], [])]),
    ('<p tal:content="default">d</p>', [('default', [], [])]),
    ('<p tal:content="translate(\'x\')">d</p>', [("translate('x')", ['__translate'], [])]),
    ('<p tal:content="exists: a | b">d</p>', [('exists: a | b', ['a', 'b'], [])]),
    ('<p tal:content="import: os">d</p>', [('import: os', [], [])]),
    ('<p tal:content="(lambda q: q + z)(1)">d</p>', [('(lambda q: q + z)(1)', ['z'], [])]),
    ('<p tal:content="a | [i for i in b]">d</p>', [('a | [i for i in b]', ['a', 'b', 'i'], ['i'])]),
    ('<p tal:define="v x" tal:content="v">d</p>', [('x', ['x'], []), ('v', ['v'], [])]),
    ('<p a="${x}${y.z}">d</p>', [('${x}${y.z}', ['x', 'y'], [])]),
    (
        '<p tal:repeat="i items">${repeat.i.index}</p>',
        [('items', ['items'], []), ('${repeat.i.index}', ['repeat'], [])],
    ),
]


@pytest.mark.parametrize(('source', 'expected'), FREE)
def test_free_variables(source: str, expected: list[Any]) -> None:
    assert _testing.free_variables(source) == expected


def test_free_variables_after_a_deferred_error() -> None:
    assert _testing.free_variables('<p tal:content="1 +">d</p>', strict=False) == [('1 +', [], [])]


@pytest.mark.parametrize(
    'source',
    [
        '<p tal:content="econtext + 1">x</p>',
        '<p tal:content="1 + econtext">x</p>',
        '<p tal:content="default + 1">x</p>',
    ],
)
def test_type_names_in_messages(source: str) -> None:
    # CPython names the type in the message: Chameleon's scope is `Scope`, its default
    # marker an `ImportableMarker`; ours are native classes renamed to match.
    with pytest.raises(TypeError) as want:
        chameleon.PageTemplate(source)()
    with pytest.raises(TypeError) as got:
        chameleon_rs.PageTemplate(source)()
    assert got.value.args == want.value.args
    assert "'Scope'" in str(got.value) or "'ImportableMarker'" in str(got.value)


def _hijack(*args: Any, **kwargs: Any) -> str:
    return 'HIJACK'


CODE_BLOCK_BUILTINS = [
    ('<div><?python y = len("abc") ?>${y}</div>', {'len': _hijack}),
    ('<?python def f(a): return len(a) ?>${python: f("abc")}', {'len': _hijack}),
    ('<?python y = [str(i) for i in [1]] ?>${y}', {'str': _hijack}),
    ('<?python y = (lambda s: sorted(s))([2, 1]) ?>${y}', {'sorted': _hijack}),
    ('<?python y = isinstance(1, int) ?>${y}', {'isinstance': _hijack, 'int': str}),
    ('<div>${len("abc")}</div>', {'len': _hijack}),
    ('<?python len = 5 ?>${len}<?python y = len("ab") ?>${y}', {}),
    ('<?python len = 5 ?>${len}', {}),
    ('<?python len = 5 ?>${len is not None}', {'len': _hijack}),
    ('<?python len = 5 ?><p tal:content="python: len + 1"/>', {}),
    # A `def`, a parameter or an import named like a builtin stays the template's own: data
    # named `filter` once replaced the template's `filter()`.
    ('<?python\ndef filter(seq):\n    return [i for i in seq if i]\n?>${filter([0, 1])}', {'filter': 'x'}),
    ('<?python def id(x): return "T" ?>${id(1)}', {'id': _hijack}),
    ('<div metal:define-macro="m"><?python def id(x): return "T" ?>${id(1)}</div>', {'id': _hijack}),
    ('<?python def f(max): return 1 ?>${max(1, 2)}', {'max': _hijack}),
    ('<?python import os as len ?>${len is not None}', {'len': _hijack}),
    ('<?python len = 5 ?><?python def len(x): return "D" ?>${len(1)}', {'len': _hijack}),
]


@pytest.mark.parametrize(('source', 'data'), CODE_BLOCK_BUILTINS)
def test_render_data_cannot_replace_builtins_in_code_blocks(source: str, data: dict[str, Any]) -> None:
    # Security: Chameleon's code blocks call builtins directly; only expressions look them up in
    # the scope first (`get('len', len)`). Render data named `len` must not reach a block.
    def outcome(engine: Any) -> str:
        try:
            return engine.PageTemplate(source)(**data)
        except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
            return f'{type(e).__name__}: {str(e).splitlines()[0]}'

    assert outcome(chameleon_rs) == outcome(chameleon)
    assert 'HIJACK' not in outcome(chameleon_rs) or '${len(' in source


class _Shape:
    __match_args__ = ('x',)
    KIND = 'k'

    def __init__(self, x: int) -> None:
        self.x = x


class _ShapeNames:
    Shape = _Shape


CODE_BLOCK_BINDINGS = [
    ('<?python\ntry:\n    1 / 0\nexcept Exception as err:\n    x = str(err)\n?>${x}', {}, 'division by zero'),
    # After the handler the name is unbound, as in Python: the scope's again.
    (
        '<?python\ntry:\n    1 / 0\nexcept Exception as err:\n    x = type(err)\n?>${x.__name__} ${err}',
        {'err': 'data'},
        'ZeroDivisionError data',
    ),
    ('<?python\ntry:\n    1 / 0\nexcept* ZeroDivisionError as group:\n    x = len(group.exceptions)\n?>${x}', {}, '1'),
    (
        '<?python\nclass Greeter:\n    def hello(self, name):\n        return "hi " + name\n?>${Greeter().hello("a")}',
        {},
        'hi a',
    ),
    ('<?python\nasync def later():\n    return 1\n?>${later.__name__}', {}, 'later'),
    (
        '<?python\nmatch [1, 2, 3]:\n    case [a, *rest]:\n        y = a + len(rest)\n?>${y} ${a} ${rest}',
        {},
        '3 1 [2, 3]',
    ),
    ('<?python\nmatch 5:\n    case int() as n if n > 3:\n        y = n + 1\n?>${y} ${n}', {}, '6 5'),
    (
        '<?python\nmatch {"k": 1, "z": 2}:\n    case {"k": v, **others}:\n        pass\n?>${v} ${others}',
        {},
        "1 {'z': 2}",
    ),
    # Chameleon renders `7`: its `NameError` for `C` is what the `|` falls back from.
    ('<?python class C: pass ?>${C.__name__ | 7}', {}, 'C'),
    # A class body is Python's: its assignments are the class's attributes (in Chameleon they
    # are stored in the template scope: `${v}` is 3 there, and `C.v` an `AttributeError`).
    ('<?python\nclass C:\n    v = 3\n    w = v + 1\n?>${C.v} ${C.w} ${v | "no v"}', {}, '3 4 no v'),
    (
        '<?python\nfrom dataclasses import dataclass\n@dataclass\nclass P:\n    x: int\n    y: int = 2\n?>${P(1)}',
        {},
        'P(x=1, y=2)',
    ),
    # A method doesn't see the class's names, and what it assigns is the template's, as in a `def`.
    (
        '<?python\nclass C:\n    v = 3\n    def m(self):\n        seen = v\n        return self.v + seen\n?>${C().m()} ${seen}',
        {'v': 10},
        '13 10',
    ),
    ('<?python\nclass C:\n    def m(self):\n        return 1\n?>${m | "no m"}', {}, 'no m'),
    ('<?python\nclass A:\n    class B:\n        v = 1\n    w = B.v + 1\n?>${A.B.v} ${A.w}', {}, '1 2'),
    (
        (
            '<?python\nclass C:\n    def __init__(self, n):\n        self.__n = n\n'
            '    def double(self):\n        return self.__n * 2\n?>${C(4).double()} ${C(1)._C__n}'
        ),
        {},
        '8 1',
    ),
    # A template builtin read in a method (the generated code's own names aren't mangled).
    ('<?python\nclass C:\n    def m(self):\n        return nothing is None\n?>${C().m()}', {}, 'True'),
    # `import a.b` binds `a` (Chameleon records `a.b`, so reading `os` is a `NameError` there).
    ('<?python import os.path ?>${os.path.basename("a/b")}', {}, 'b'),
    # An import in a class body is the class's: the name is still the scope's outside it.
    ('<?python\nclass C:\n    import os\n?>${os} ${C.os.sep}', {'os': 'data'}, 'data /'),
    # A class or value of a pattern that is the scope's (`SyntaxError` in Chameleon: a pattern
    # takes a dotted name, not the lookup a read is rewritten to), looked up when tried.
    ('<?python\nmatch p:\n    case Shape(x=a):\n        y = a\n?>${y}', {'p': _Shape(3), 'Shape': _Shape}, '3'),
    ('<?python\nmatch p:\n    case int():\n        y = 1\n    case Missing():\n        y = 2\n?>${y}', {'p': 5}, '1'),
    ('<?python\nmatch c:\n    case ns.Shape.KIND:\n        y = "kind"\n?>${y}', {'c': 'k', 'ns': _ShapeNames}, 'kind'),
    # In a class body the capture is the class's, as any name bound there: not the scope's.
    (
        '<?python\nclass C:\n    match p:\n        case Shape(x=a):\n            found = a\n?>${C.found} ${C.a} ${a}',
        {'p': _Shape(3), 'Shape': _Shape, 'a': 'data'},
        '3 3 data',
    ),
    # Type parameters are visited like the rest of a `def` or `class` (a bound from the scope).
    (
        (
            '<?python\ndef f[T: Shape](a: T) -> T:\n    return a\nclass C[U: Shape]:\n    pass\n?>'
            '${f.__type_params__[0].__bound__.__name__} ${C.__type_params__[0].__bound__.__name__}'
        ),
        {'Shape': _Shape},
        '_Shape _Shape',
    ),
    # A case that doesn't match binds nothing: the name is still the scope's.
    (
        '<?python\nmatch 5:\n    case str() as n:\n        pass\n    case _:\n        pass\n?>${n}',
        {'n': 'data'},
        'data',
    ),
    # A capture assigned in the body is the scope's name, as any assignment.
    ('<?python\nmatch 5:\n    case n:\n        n = n * 2\n?>${n}', {}, '10'),
]


@pytest.mark.parametrize(('source', 'data', 'expected'), CODE_BLOCK_BINDINGS)
def test_code_block_names_bound_without_an_assignment(source: str, data: dict[str, Any], expected: str) -> None:
    # `except ... as`, `class`, `async def` and a `match` capture bind a name that Chameleon's
    # name rewriting misses: reading it there is a `NameError` (unless render data has the
    # name, which is then read instead). Here the name is the one the block bound.
    assert chameleon_rs.PageTemplate(source)(**data) == expected


BINDING_STATEMENTS = [
    ('class thing: pass', 'thing'),
    ('def thing(): pass', 'thing'),
    ('async def thing(): pass', 'thing'),
    ('import os as thing', 'os'),
]


@pytest.mark.parametrize(('statement', 'name'), BINDING_STATEMENTS)
def test_code_block_names_are_not_bound_in_another_macro(statement: str, name: str) -> None:
    # Each macro, and the main body, is a function of its own in Chameleon: what a code block
    # binds in one is not a name of the next. Chameleon's one visitor kept those names for
    # every macro after, a `NameError` there where the render data has the name (fixed
    # upstream in malthe/chameleon#465, as here).
    source = (
        f'<div metal:define-macro="first"><?python\n{statement}\n?>${{thing.__name__}}</div>'
        '<div metal:define-macro="second">${thing}</div>'
        '<div metal:use-macro="template.macros[\'first\']" />'
        '<div metal:use-macro="template.macros[\'second\']" />'
        '${thing}'
    )
    # Each macro is rendered where it is defined, and once more where it is used.
    expected = 2 * f'<div>{name}</div><div>data</div>' + 'data'
    assert chameleon_rs.PageTemplate(source)(thing='data') == expected


@pytest.mark.parametrize(('statement', 'name'), BINDING_STATEMENTS)
def test_code_block_exception_name_that_is_bound_already(statement: str, name: str) -> None:
    # A handler unbinds its name. One a `def`, a class or an import bound before stays a plain
    # name, so reading it after the handler ran was an `UnboundLocalError` (in Chameleon too,
    # fixed in malthe/chameleon#465): it is the scope's then, and the earlier binding if the
    # handler did not run.
    source = (
        f'<?python\n{statement}\ntry:\n    %s\nexcept ZeroDivisionError as thing:\n'
        '    seen = type(thing).__name__\n?>${seen} ${getattr(thing, "__name__", thing)}'
    )
    ran = chameleon_rs.PageTemplate(source % '1 / 0')
    assert ran(seen='-', thing='data') == 'ZeroDivisionError data'
    with pytest.raises(NameError):
        ran(seen='-')
    assert chameleon_rs.PageTemplate(source % 'pass')(seen='-', thing='data') == f'- {name}'


@pytest.mark.parametrize(
    ('block', 'expected'),
    [
        # Once per run of the handler: skipped, then run.
        (
            (
                'out = []\nfor n in (1, 0):\n    try:\n        1 / n\n    except ZeroDivisionError as thing:\n'
                '        pass\n    out.append(getattr(thing, "__name__", thing))\nres = out'
            ),
            "['thing', 'data']",
        ),
        # The `finally` of the statement reads the scope's name too, and its `else` the function.
        ('try:\n    1 / 0\nexcept ZeroDivisionError as thing:\n    pass\nfinally:\n    res = thing', 'data'),
        ('try:\n    pass\nexcept ZeroDivisionError as thing:\n    pass\nelse:\n    res = thing.__name__', 'thing'),
        (
            'try:\n    [][0]\nexcept ZeroDivisionError as thing:\n    pass\nexcept IndexError:\n    pass\nres = thing.__name__',
            'thing',
        ),
        (
            'try:\n    raise ExceptionGroup("g", [ValueError(1)])\nexcept* ValueError as thing:\n    pass\nres = thing',
            'data',
        ),
        # Inside a function, where the name is its local.
        (
            (
                'def f():\n    def thing(): pass\n    try:\n        1 / 0\n    except ZeroDivisionError as thing:\n'
                '        pass\n    return thing\nres = f()'
            ),
            'data',
        ),
    ],
)
def test_code_block_exception_name_is_rebound_on_every_path(block: str, expected: str) -> None:
    source = f'<?python\ndef thing(): pass\n{block}\n?>${{res}}'
    assert chameleon_rs.PageTemplate(source)(thing='data') == expected


class _Point:
    __match_args__ = ('x',)

    def __init__(self, x: int) -> None:
        self.x = x


class _Kind:
    A = 99


CLASS_DATA: dict[str, Any] = {
    'a': 100,
    'x': 99,
    'v': [3, 4],
    'items': [1, 2],
    'p': _Point(5),
    'p2': _Point(6),
    'Point': _Point,
    'Kind': _Kind,
    'Bound': int,
    'Fault': ZeroDivisionError,
    'deco': lambda function: function,
}

# Each sets `res`. The class's own names shadow render data of the same name (`a`, `x`, `items`).
CLASS_BODIES = [
    # A comprehension sees the class's names in its first iterable only.
    'class C:\n    a = 1\n    items = [10, 20]\n    b = [a + i for i in items]\nres = C.b',
    'class C:\n    a = 1\n    items = [10, 20]\n    b = {a + i for i in items}\nres = sorted(C.b)',
    'class C:\n    a = 1\n    items = [10, 20]\n    b = {i: a for i in items}\nres = C.b',
    'class C:\n    a = 1\n    items = [10, 20]\n    b = list(a + i for i in items)\nres = C.b',
    'class C:\n    a = 1\n    items = [10, 20]\n    b = [(i, j) for i in items for j in [a]]\nres = C.b',
    'class C:\n    a = 1\n    items = [10, 20]\n    b = [i for i in items if i > a]\nres = C.b',
    'class C:\n    items = [10, 20]\n    b = [[a + j for j in items] for i in items]\nres = C.b',
    'class C:\n    a = 1\n    b = [(lambda: a)() for i in items]\nres = C.b',
    'class C:\n    x = 1\n    y = [x for x in items]\n    z = x\nres = (C.y, C.z)',
    'class C:\n    n = 5\n    t = tuple(n for _ in range(n))\nres = C.t',
    'class C:\n    import os.path\n    sep = [os.sep for _ in items]\nres = 1',
    # Defaults are evaluated in the class body, a lambda's or a method's body is not.
    'class C:\n    x = 10\n    f = lambda self, y=x: (y, x)\nres = C().f()',
    'class C:\n    x = 10\n    f = lambda self, *, y=x: (y, x)\nres = C().f()',
    'class C:\n    x = 10\n    def f(self, y=x):\n        return (y, x)\nres = C().f()',
    'class C:\n    def f(self, a=a):\n        return a\n    g = lambda self, x=x: x\nres = (C().f(), C().g())',
    'class C:\n    a = 1\n    f = lambda self, a=a, b=[a for _ in items]: (a, b)\nres = C().f()',
    'class C:\n    x = 1\n    y = (lambda: x)()\nres = C.y',
    'class C:\n    x = 10\n    def f(self):\n        return [x + i for i in items]\nres = C().f()',
    # Classes in classes, and in methods.
    'class C:\n    x = 10\n    class D:\n        y = x\n        z = [x for _ in items]\nres = (C.D.y, C.D.z)',
    (
        'class C:\n    x = 10\n    def f(self):\n        class D:\n            x = 1\n            y = x + a\n'
        '            g = lambda self, z=x: (z, x)\n        return (D.y, D().g())\nres = C().f()'
    ),
    'def deco(c):\n    c.tag = a\n    return c\n@deco\nclass C:\n    a = 1\nres = (C.tag, C.a)',
    # A `match` capture is the class's name, and the scope's `a` is left alone.
    'class C:\n    match v:\n        case [a, b]:\n            y = a + b\nres = (C.y, a, getattr(C, "a", "MISSING"))',
    'class C:\n    match v:\n        case [a, b] if a < b:\n            y = [a for _ in items]\nres = (C.y, a, C.a)',
    # Nothing of ours is left on the class.
    (
        'class C:\n    match p:\n        case Point(x=q):\n            y = q\n'
        'res = (C.y, sorted(k for k in vars(C) if k.startswith("__rs")))'
    ),
    'class C:\n    def f(self):\n        match p:\n            case Point(x=q):\n                return q + a\nres = C().f()',
    # A class attribute named `globals` doesn't hide what the generated code reads.
    'class C:\n    globals = 1\n    t = template\n    def f(self):\n        return template\nres = (C.t is C().f(), C.globals)',
    'class C:\n    globals = 1\n    match p:\n        case Point(x=q):\n            y = q\nres = C.y',
    # ... with one `match` in another, and when its body is left early.
    (
        'class C:\n    match p:\n        case Point(x=q):\n            match p2:\n'
        '                case Point(x=r):\n                    y = q + r\nres = C.y'
    ),
    (
        'class C:\n    for item in [p]:\n        match item:\n            case Point(x=q):\n'
        '                y = q\n                break\nres = (C.y, [k for k in vars(C) if k.startswith("__rs")])'
    ),
    (
        'class C:\n    try:\n        match p:\n            case Point(x=q):\n                1 / 0\n'
        '    except ZeroDivisionError:\n        pass\nres = [k for k in vars(C) if k.startswith("__rs")]'
    ),
    # A class of the body itself in a pattern.
    'class C:\n    class Q:\n        pass\n    match Q():\n        case Q():\n            y = 1\nres = C.y',
    # A method's parameters are its own: not names of the class body or of the template after it.
    (
        'class C:\n    def f(self, a=a):\n        return a\n    def g(self, a=a):\n        return a\n'
        'res = (C().f(), C().g())'
    ),
    'class C:\n    def f(self, a):\n        return a\n    y = a\nres = (C().f(1), C.y)',
    'class C:\n    def f(self, a):\n        return a\nres = (C().f(1), a)',
    (
        'class C:\n    def f(self):\n        def inner(a):\n            return a\n        return inner(1)\n'
        'res = (C().f(), a)'
    ),
    # A walrus in what a function, a lambda or a class evaluates in the body binds the class's name.
    'class C:\n    f = lambda self, y=(k := 10): y\nres = (C().f(), C.k)',
    'class C:\n    def f(self, y=(k := 10)):\n        return y\nres = (C().f(), C.k)',
    'class C:\n    @(k := deco)\n    def f(self):\n        return 1\nres = (C().f(), C.k is deco)',
    'class C:\n    class D(k := object):\n        pass\nres = C.k is object',
    # A name the body binds is the template's while it isn't bound.
    'class C:\n    a = 1\n    del a\n    b = a\nres = C.b',
    'class C:\n    b = a\n    a = 1\nres = (C.b, C.a)',
    'class C:\n    if False:\n        a = 1\n    b = a\nres = C.b',
    'class C:\n    a: int\n    b = a\nres = C.b',
    'class C:\n    del zz\nres = 1',
    # `global`: the template's name.
    'class C:\n    global a\n    a = 42\nres = a',
    'class C:\n    global a\n    def a():\n        return 42\nres = (a(), hasattr(C, "a"))',
    'class C:\n    global a\n    class a:\n        v = 7\nres = (a.v, hasattr(C, "a"))',
    'class C:\n    global a\n    import math as a\nres = (a.pi, hasattr(C, "a"))',
    'class C:\n    global a\n    def a():\n        return 42\n    b = a()\n    del a\nres = C.b',
    'class C:\n    global a\n    a = 42\n    del a\nres = hasattr(C, "a")',
    # A `match` in a class inside a function that has a `match` of its own, taken or not.
    (
        'def f():\n    class C:\n        match p:\n            case Point(x=q):\n                y = q\n'
        '    match p2:\n        case Point(x=r):\n            z = r\n    return (C.y, z)\nres = f()'
    ),
    (
        'def f():\n    class C:\n        match p:\n            case Point(x=q):\n                y = q\n'
        '    if False:\n        match p2:\n            case Point(x=r):\n                z = r\n    return C.y\nres = f()'
    ),
    # A pattern's class or value the class body also binds: the class's once it is bound.
    'class C:\n    match p:\n        case Point(x=q):\n            Point = q\nres = C.Point',
    'class C:\n    match p:\n        case Point(x=q):\n            y = q\n    Point = int\nres = (C.y, C.Point)',
    'class C:\n    Point = int\n    del Point\n    match p:\n        case Point(x=q):\n            y = q\nres = C.y',
    (
        'class C:\n    Point = int\n    match p:\n        case Point(x=q):\n            y = q\n        case _:\n'
        '            y = "no"\nres = C.y'
    ),
    'class C:\n    match x:\n        case Kind.A:\n            y = 1\n    Kind = object\nres = C.y',
    # A name is bound after the header that defines it is evaluated, in a class body too.
    'class C:\n    class Point(Point):\n        pass\n    ok = Point.__mro__[1].__name__\nres = C.ok',
    'class C:\n    try:\n        1 / 0\n    except Fault as Fault:\n        y = type(Fault).__name__\nres = C.y',
    'class Point(Point):\n    pass\nres = Point.__mro__[1].__name__',
    '@deco\nclass deco(Point, metaclass=type(deco and Point)):\n    pass\nres = deco.__mro__[1].__name__',
    'try:\n    1 / 0\nexcept Fault as Fault:\n    y = type(Fault).__name__\nres = y',
    'try:\n    1 / 0\nexcept (Fault, ValueError) as Fault:\n    y = type(Fault).__name__\nres = y',
    (
        '@deco\nasync def deco(x=deco, *, y=deco) -> deco:\n    return x\n'
        'res = (deco.__defaults__[0] is deco, deco.__kwdefaults__["y"] is deco)'
    ),
    (
        'class C:\n    def m(self):\n        async def g(x=1):\n            return x\n        return g.__name__\n'
        'res = C().m()'
    ),
    # A parameter named like its `async def` method doesn't make that name the template's.
    'class C:\n    async def a(self, a=1):\n        return a\nres = a',
    (
        'class C:\n    async def a(self, *, a=1):\n        return a\n    y = a\n    def g(self):\n        return a\n'
        'res = (C.y is C.a, C().g())'
    ),
    # `global` in a method, or in a class inside one: the name outlives the method.
    (
        'class C:\n    def m(self):\n        class D:\n            global a\n            def a():\n                return 42\n'
        'C().m()\nres = a()'
    ),
    'class C:\n    def m(self):\n        global a\n        def a():\n            return 42\nC().m()\nres = a()',
    'class C:\n    def m(self):\n        global a\n        class a:\n            v = 7\nC().m()\nres = a.v',
    'class C:\n    def m(self):\n        global a\n        import math as a\nC().m()\nres = a.pi',
    'class C:\n    def m(self):\n        global a\n        a = 42\n        del a\nC().m()\nres = "ok"',
    # A `global` function's decorators and defaults are evaluated before its name is bound.
    'class C:\n    global a\n    def a(x=a):\n        return x\nres = a()',
    'class C:\n    global deco\n    @deco\n    def deco():\n        return 7\nres = deco()',
    # A class's name isn't bound while its body runs; its methods run later and read it.
    (
        'class Point:\n    b = Point\n    def m(self):\n        return Point\n'
        'res = (Point.b.__name__, Point().m() is Point)'
    ),
    'class Point:\n    class Point(Point):\n        pass\nres = Point.Point.__mro__[1].__name__',
    'class Point:\n    def m(self, x=Point):\n        return x\nres = Point().m().__name__',
    'class Point:\n    xs = [Point for _ in [1]]\nres = Point.xs[0].__name__',
    'class K:\n    v = 1\nclass K:\n    old = K.v\n    v = 2\nres = (K.old, K.v)',
    'class Zed:\n    b = Zed\nres = 1',
    'def f():\n    class Point:\n        b = Point\n    return Point.b\nres = f()',
    'class C:\n    class D:\n        def m(self):\n            return D\nres = C.D().m()',
    (
        'class Point:\n    def m(self):\n        class Inner:\n            b = Point\n        return Inner.b\n'
        'res = Point().m() is Point'
    ),
    # A type parameter's bound is evaluated once the class is bound.
    'class Q[T: Q]:\n    pass\nres = Q.__type_params__[0].__bound__ is Q',
    (
        'class Point:\n    def m[T: Point](self, x: T) -> T:\n        return x\n'
        'res = Point.m.__type_params__[0].__bound__ is Point'
    ),
    # A `global` binding that never runs changes nothing.
    'class C:\n    def m(self):\n        global a\n        def a():\n            return 42\n    y = a\nres = (C.y, a)',
    (
        'class C:\n    def m(self):\n        global a\n        def a():\n            return 42\n'
        '    def other(self):\n        return a\nres = C().other()'
    ),
    'class C:\n    if False:\n        global a\n        def a():\n            return 1\nres = a',
    (
        'class C:\n    def m(self):\n        global a\n        def a():\n            return 42\n'
        '    def other(self):\n        return a()\nC().m()\nres = (C().other(), a())'
    ),
    # `global` in a function whose enclosing method has a parameter of that name.
    (
        'class C:\n    def m(self, a):\n        def inner():\n            global a\n            return a\n'
        '        return (a, inner())\nres = C().m(10)'
    ),
    (
        'class C:\n    def m(self, a):\n        def inner():\n            global a\n            a = 55\n'
        '        inner()\n        return a\nres = (C().m(10), a)'
    ),
    (
        'class C:\n    def m(self, a):\n        def inner():\n            global a\n            def a():\n'
        '                return 7\n        inner()\n        return a\nres = (C().m(10), a())'
    ),
    (
        'class C:\n    def m(self, a):\n        def inner():\n            global a\n'
        '            return (lambda a: a)(3), (lambda: a)()\n        return inner()\nres = C().m(10)'
    ),
    # A `global` class named like the class around it, and other `global` bindings.
    (
        'class Point:\n    global Point\n    class Point:\n        b = Point\n    saved = Point.b\n'
        'res = Point.saved.__name__'
    ),
    (
        'def a():\n    return 1\nclass C:\n    def m(self):\n        global a\n        def a():\n            return 2\n'
        'r1 = a()\nC().m()\nres = (r1, a())'
    ),
    'class C:\n    global a\n    from math import pi as a, e as x\n    b = a\nres = (C.b, a, x)',
    (
        'class C:\n    def m(self):\n        global a, x\n        import os.path as a, math as x\n'
        'C().m()\nres = (a.sep, x.pi)'
    ),
    (
        'class C:\n    def m(self):\n        global a\n        class a:\n            v = 7\n            def me(self):\n'
        '                return a\nC().m()\nres = (a.v, a().me() is a)'
    ),
    # A builtin declared `global` and defined: the block's own, and the builtin again after `del`.
    (
        'class C:\n    global len\n    def len():\n        return 42\n    r1 = len()\n    del len\n'
        'res = (C.r1, len([1, 2, 3]))'
    ),
    'class C:\n    global len\n    def len():\n        return 42\nres = len()',
    'class C:\n    def m(self):\n        global len\n        def len():\n            return 42\nres = len([1])',
    # `global` in a class inside a function with a parameter of that name.
    'def f(a):\n    class C:\n        global a\n        b = a\n    return (a, C.b)\nres = f(10)',
    (
        'def f(a):\n    class C:\n        def m(self):\n            global a\n            return a\n'
        '    return (a, C().m())\nres = f(10)'
    ),
    # Type parameters are names of the class or function; a bound doesn't see the parameters.
    (
        'class C:\n    def m[T: Bound](self, Bound):\n        return Bound\n'
        'res = (C().m(5), C.m.__type_params__[0].__bound__.__name__)'
    ),
    'class Box[T](list[T]):\n    kind = T\nres = Box.kind is Box.__type_params__[0]',
    'class C:\n    def m[T](self):\n        return T\nres = C().m() is C.m.__type_params__[0]',
    (
        'class Box[a]:\n    kind = a\n    def get(self):\n        return a\n'
        'res = (Box.kind is Box.__type_params__[0], Box().get() is Box.__type_params__[0], a)'
    ),
    'class Box[T, U: T]:\n    pass\nres = Box.__type_params__[1].__bound__ is Box.__type_params__[0]',
    (
        'def f[T: Bound, *Ts, **P](x: T) -> T:\n    return x\n'
        'res = (f(3), f.__type_params__[0].__bound__.__name__, len(f.__type_params__))'
    ),
    # A bound in a class body reads the finished class's names.
    'class C:\n    class Inner[T: Inner]:\n        pass\nres = C.Inner.__type_params__[0].__bound__ is C.Inner',
    'class C:\n    Bound = "attr"\n    class Inner[T: Bound]:\n        pass\nres = C.Inner.__type_params__[0].__bound__',
    'class C:\n    Bound = str\n    def m[T: Bound](self):\n        return T.__bound__.__name__\nres = C().m()',
    # A default is evaluated outside the type parameters' scope.
    'def f[a](x=a):\n    return x\nres = f()',
    'class C:\n    def m[a](self, x=a, *, y=a):\n        return (x, y, a.__name__)\nres = C().m()',
    'class C[T]:\n    def m[U](self, x=a):\n        return (x, T.__name__, U.__name__)\nres = C().m()',
    # A type parameter named like its class or function.
    'class Box[Box]:\n    k = Box\nres = (Box.__name__, type(Box.k).__name__)',
    'class Box[Box]:\n    pass\nclass D(Box):\n    pass\nres = D.__mro__[1].__name__',
    'async def f[f]():\n    return 1\nres = f.__name__',
    'def f[f]():\n    return f\nres = (f.__name__, type(f()).__name__)',
    'class C:\n    def m(self):\n        async def f[f]():\n            return 1\n        return f.__name__\nres = C().m()',
    # A name the class body binds is not its class's type parameter of that name.
    'class Box[a]:\n    x = a\n    a = 1\nres = (Box.x, Box.a)',
    (
        'class Box[a]:\n    x = a\n    a = 1\n    def get(self):\n        return a\n'
        'res = (Box.x, type(Box().get()).__name__)'
    ),
    'class Box[a]:\n    a = 1\n    del a\n    x = a\nres = Box.x',
    # `type` statements.
    'type Alias[T] = list[T]\nres = (Alias.__name__, repr(Alias.__value__))',
    'type Alias = list[Bound]\nres = Alias.__value__',
    'type Alias[a] = list[a]\nres = (Alias.__name__, a)',
    'class C:\n    type Alias[T] = list[T]\n    v = repr(Alias.__value__)\nres = C.v',
    'class C:\n    Y = str\n    type Alias = list[Y]\n    v = Alias.__value__\nres = C.v',
    'class C:\n    def m(self):\n        type Alias = list[Bound]\n        return Alias.__value__\nres = C().m()',
    'class C:\n    global Alias\n    type Alias = int\nres = Alias.__value__',
    'type Alias = int\nclass C:\n    v = Alias\nres = C.v.__name__',
    # A `type` statement's value and bounds read the alias itself: its name is bound first.
    'type Tree = list[Tree]\nres = Tree.__value__ == list[Tree]',
    'type Tree[T] = T | list[Tree[T]]\nres = repr(Tree.__value__)',
    'type A[T: A] = list[T]\nres = A.__type_params__[0].__bound__ is A',
    'type A[T = A] = list[T]\nres = A.__type_params__[0].__default__ is A',
    'type Point = Point\nres = Point.__value__ is Point',
    'def f():\n    type Tree = list[Tree]\n    return Tree.__value__ == list[Tree]\nres = f()',
    'class C:\n    def m(self):\n        type Tree = list[Tree]\n        return Tree.__value__ == list[Tree]\nres = C().m()',
    (
        'class C:\n    def m(self):\n        global Tree\n        type Tree = list[Tree]\n'
        'C().m()\nres = Tree.__value__ == list[Tree]'
    ),
    'type T[T] = list[T]\nres = (T.__name__, repr(T.__value__))',
    # A name the class body binds is, before that, the template's: never a type parameter, a
    # parameter or a local of what is around the class.
    'class Outer[a]:\n    class Inner:\n        x = a\n        a = 1\nres = (Outer.Inner.x, Outer.Inner.a)',
    'class Outer[a]:\n    class Inner[a]:\n        x = a\n        a = 1\nres = (Outer.Inner.x, Outer.Inner.a)',
    (
        'class Outer[a]:\n    def m(self):\n        class Inner:\n            x = a\n            a = 1\n'
        '        return (Inner.x, Inner.a)\nres = Outer().m()'
    ),
    'def f[a]():\n    class Inner:\n        x = a\n        a = 1\n    return (Inner.x, Inner.a)\nres = f()',
    (
        'class Outer:\n    def m(self, a):\n        class Inner:\n            x = a\n            a = 1\n'
        '        return (Inner.x, Inner.a)\nres = Outer().m(42)'
    ),
    'def f(a):\n    class C:\n        x = a\n        a = 1\n    return (C.x, C.a)\nres = f(42)',
    (
        'def f():\n    def a():\n        pass\n    class C:\n        x = a\n        a = 1\n'
        '    return (C.x, C.a)\nres = f()'
    ),
    (
        'class O:\n    def m(self):\n        try:\n            1 / 0\n        except Fault as a:\n'
        '            class C:\n                x = a\n                a = 1\n            return (C.x, C.a)\n'
        'res = O().m()'
    ),
    (
        'class O:\n    def m(self, Kind):\n        class C:\n            match 5:\n                case Kind():\n'
        '                    r = 1\n                case _:\n                    r = 2\n            Kind = 1\n'
        '        return C.r\nres = O().m(int)'
    ),
    'def a():\n    return 3\nclass Box[a]:\n    x = a\n    a = 1\nres = (Box.x(), Box.a)',
    # `global` of a name that is a type parameter of the class around it.
    (
        'class Box[a]:\n    def m(self):\n        global a\n        old = a\n        a = 200\n        return old\n'
        'res = (Box().m(), a)'
    ),
    'class Box[a]:\n    global a\n    x = a\n    a = 200\nres = (Box.x, a)',
    'class Box[a]:\n    global a\n    del a\nres = a',
    'class Box[a]:\n    def m(self):\n        global a\n        del a\nBox().m()\nres = a',
    'class Box[a]:\n    def m(self):\n        global a\n        def a():\n            return 7\nBox().m()\nres = a()',
    'class Box[a]:\n    def m(self):\n        global a\n        type a = int\nBox().m()\nres = a.__value__',
    'class Box[Box]:\n    def m(self):\n        global Box\n        return Box.__name__\nres = Box().m()',
    (
        'class C:\n    try:\n        1 / 0\n    except Fault as a:\n        pass\n'
        '    def m(self):\n        global a\n        return a\nres = C().m()'
    ),
    # `global` of the class being defined, in one of its methods: the class, bound by then.
    'class Base:\n    def m(self):\n        global Base\n        return Base.__name__\nres = Base().m()',
    'class Point:\n    def m(self):\n        global Point\n        return Point.__name__\nres = Point().m()',
    'class Base:\n    def m(self):\n        global Base\n        Base = 99\nBase().m()\nres = Base',
    (
        'class Base:\n    class Inner:\n        def m(self):\n            global Base\n            Base = 99\n'
        'Base.Inner().m()\nres = Base'
    ),
    'class Base:\n    class Inner:\n        global Base\n        type X = Base\nres = Base.Inner.X.__value__ is Base',
    (
        'class Base:\n    def m(self):\n        class Inner:\n            x = Base\n            Base = 1\n'
        '        return Inner.x\nres = Base().m().__name__'
    ),
    # A comprehension's targets in a class body are its own names, not stores in the scope.
    'class C:\n    values = [a for a in items]\nres = (a, C.values)',
    'class C:\n    values = {a: x for a, x in enumerate(items)}\nres = (a, x, C.values)',
    'class C:\n    values = [a + i for a in items for i in items if a]\nres = (a, C.values)',
    'class C:\n    a = 5\n    values = list(a for a in items)\nres = (C.a, C.values, a)',
    # A class attribute or a parameter named `econtext` doesn't hide the scope from lookups.
    'class C:\n    econtext = "test"\n    value = a\nres = (C.value, C.econtext)',
    'class C:\n    def m(self, econtext):\n        return (a, econtext)\nres = C().m(1)',
    # A name in the class's namespace that no statement of the body binds.
    'class C:\n    locals()["label"] = "Ready"\n    value = label\nres = C.value',
    'class C:\n    locals()["a"] = "Ready"\n    value = a\nres = (C.value, a)',
    (
        'class C:\n    locals()["Kind"] = int\n    match 5:\n        case Kind():\n            r = 1\n'
        '        case _:\n            r = 2\nres = C.r'
    ),
    # `nonlocal` names a local of the function that binds it.
    (
        'class C:\n    def method(self):\n        n = 0\n        def inc():\n            nonlocal n\n'
        '            n += 1\n        inc()\n        return n\nres = C().method()'
    ),
    (
        'class C:\n    def method(self, n):\n        def inc():\n            nonlocal n\n            n += a\n'
        '        inc()\n        return n\nres = (C().method(1), a)'
    ),
    (
        'def counter():\n    n = 0\n    def inc():\n        nonlocal n\n        n += 1\n        return n\n'
        '    return inc\ninc = counter()\nres = (inc(), inc())'
    ),
    (
        'def f():\n    x = 1\n    class C:\n        nonlocal x\n        x = 2\n        y = x\n'
        '    return (x, C.y)\nres = f()'
    ),
    # An exception target declared `global` is the template's name, unbound after the handler.
    'class C:\n    global a\n    try:\n        1 / 0\n    except Fault as a:\n        pass\nres = a',
    'class C:\n    global a\n    try:\n        1 / 0\n    except Fault as a:\n        k = type(a).__name__\nres = C.k',
    (
        'class C:\n    def m(self):\n        global a\n        try:\n            1 / 0\n'
        '        except Fault as a:\n            return type(a).__name__\nres = (C().m(), "a" in globals())'
    ),
    'class C:\n    global a\n    try:\n        pass\n    except Fault as a:\n        pass\nres = a',
    # A pattern that matches binds its captures even when its guard is false.
    (
        'class C:\n    def method(self):\n        match 42:\n            case int() as a if False:\n'
        '                pass\n        return a\nres = C().method()'
    ),
    'match 42:\n    case int() as a if a > 100:\n        pass\n    case _:\n        pass\nres = a',
    'match [1, 2]:\n    case [a, x] if a > x:\n        r = 1\n    case [a, x] if a < x:\n        r = 2\nres = (a, x, r)',
]


@pytest.mark.parametrize('block', CLASS_BODIES)
def test_class_bodies_in_code_blocks_scope_names_as_python_does(block: str) -> None:
    # Chameleon has no class scoping to compare with (reading the class is a `NameError`
    # there), so the block is run as plain Python, the render data its globals.
    def plain() -> Any:
        namespace = {**CLASS_DATA, 'template': None}
        exec(block, namespace)  # noqa: S102
        return namespace['res']

    def rendered() -> Any:
        seen: list[Any] = []
        chameleon_rs.PageTemplate(f'<?python\n{block}\n?>${{seen.append(res)}}')(**CLASS_DATA, seen=seen)
        return seen[0]

    try:
        expected = plain()
    except Exception as error:  # noqa: BLE001
        with pytest.raises(type(error)):
            rendered()
        return
    assert rendered() == expected


def test_a_failed_visit_leaves_no_lambda_scope_behind() -> None:
    class Failing(Rewriter):
        def visit_Constant(self, node: ast.Constant) -> ast.AST:
            raise RuntimeError

    rewriter = Failing({}, code_block=True)
    with pytest.raises(RuntimeError):
        rewriter.visit(ast.parse('lambda x=1: x', mode='eval'))
    assert len(rewriter.scopes) == 1


@pytest.mark.parametrize(
    'source',
    [
        '<?python\ntry:\n    x = 1\nexcept Exception as err:\n    x = 2\n?>${x}',
        '<?python\ntry:\n    1 / 0\nexcept Exception as err:\n    x = 2\n?>${x} ${err}',
        '<?python\nmatch 5:\n    case 5:\n        y = "five"\n    case _:\n        y = "other"\n?>${y}',
        '<?python\nmatch 5:\n    case int() as n:\n        y = 1\n?>${y}',
    ],
)
def test_code_blocks_chameleon_renders_are_unchanged(source: str) -> None:
    data = {'err': 'data'}
    assert chameleon_rs.PageTemplate(source)(**data) == chameleon.PageTemplate(source)(**data)


class _HtmlNone:
    __html__ = None

    def __str__(self) -> str:
        return '<hn>'


class _AnyNone:
    def __getattr__(self, name: str) -> None:
        return None

    def __str__(self) -> str:
        return '<an>'


@pytest.mark.parametrize('value', [_HtmlNone(), _AnyNone(), _HtmlNone], ids=['instance', 'getattr', 'class'])
@pytest.mark.parametrize(
    'source',
    [
        '<p>${x}</p>',
        '<p tal:content="x"/>',
        '<p title="${x}"/>',
        '<p tal:attributes="title x"/>',
        '<p tal:attributes="d"/>',
        '<p>${string:a${x}}</p>',
    ],
)
def test_an_html_attribute_of_none_falls_back_to_str(source: str, value: Any) -> None:
    # Chameleon reads `getattr(target, '__html__', None)`: `None` is no `__html__` (it falls back
    # to `str()`, escaped), never a call of `None`.
    def outcome(engine: Any) -> str:
        try:
            return engine.PageTemplate(source)(x=value, d={'title': value})
        except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
            return f'{type(e).__name__}: {str(e).splitlines()[0]}'

    if source == '<p tal:attributes="d"/>' and isinstance(value, _AnyNone):
        # Chameleon crashes here itself (its `translate` fallback gives `None`, which a
        # dictionary attribute concatenates: TypeError); ours writes an empty value.
        assert outcome(chameleon_rs) == '<p title=""/>'
        return
    assert outcome(chameleon_rs) == outcome(chameleon)


# -- Where an interpolation ends -------------------------------------------------------------

# Pieces that decide where a `${` ends, and what makes the compiler try every candidate.
BOUNDARY_PIECES = (
    ['${'] * 3
    + ['}'] * 3
    + ['{', '(', ')', '[', ']', "'", '"', 'a', 'b', ' ', '|', ':', '$', '$$', '$a', '1', '+', ',', '.', '\n']
    + ['structure:', 'string:', 'not:', 'exists:', 'python:', 'load:', 'zz:', 'lambda:', 'if', 'else', 'for q in']
    + ['#', '\\', '&amp;', '&#125;', '&quot;', 'é', 'x.y', "''", '"""', 'f"', '{}', '==']
)


@pytest.mark.parametrize('seed', range(0, 1500, 100))
def test_interpolations_end_where_chameleons_do(seed: int) -> None:
    # The longest candidate that compiles wins (`Interpolator`). Candidates that can't
    # compile are skipped unseen (`candidate_bound`), which must never change the answer.
    import random

    def outcome(engine: Any, source: str, template: str) -> str:
        try:
            return getattr(engine, template)(source)(a='A', b=[1, '<i>'], d={'k': 1, 1: 2})  # type: ignore[no-any-return]
        except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
            return f'{type(e).__name__}: {str(e).splitlines()[0]}'

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # the parser's, about the generated expressions
        for case in range(seed, seed + 100):
            pick = random.Random(case)
            body = ''.join(pick.choice(BOUNDARY_PIECES) for _ in range(pick.randint(1, 14)))
            quoted = body.replace('"', '&quot;')
            sources = [
                (f'<p>{body}</p>', 'PageTemplate'),
                (f'<p title="{quoted}"/>', 'PageTemplate'),
                (f'<p tal:content="string:{quoted}"/>', 'PageTemplate'),
                (body, 'PageTextTemplate'),
            ]
            for source, template in sources:
                assert outcome(chameleon_rs, source, template) == outcome(chameleon, source, template), source


@pytest.mark.parametrize('expression', ['a', 'a.real', 'structure: a', "d['k']", '(a, {1: 2})'])
@pytest.mark.parametrize('wrap', ['<p>{}</p>', '<p title="{}"/>', '<p tal:content="string:{}"/>'])
def test_many_interpolations_in_one_text_compile_fast(expression: str, wrap: str) -> None:
    # Every `${` once tried each `}` after it, each candidate as long as the text: 2,000 in
    # one text node took 6 s to compile (54 s in Chameleon). Now a fraction of a second; the
    # limit only tells the two apart.
    import time

    source = wrap.format(('${' + expression + '} ') * 3000)
    start = time.perf_counter()
    page = chameleon_rs.PageTemplate(source)
    assert time.perf_counter() - start < 5
    assert page(a=1, d={'k': 1}).count('1') >= 3000


@pytest.mark.parametrize(
    'source',
    [
        '${(lambda q: q or nothing)(0)}',
        '${[f(1) for f in [lambda q: template is not None]]}',
        '${(lambda: (lambda: nothing)())()}',
        '${(lambda q: q.real)(1)}',
        '<?python f = lambda q: q or nothing ?>${f(0)}',
        '<?python\ndef f():\n    return (lambda: nothing)()\n?>${f()}',
    ],
)
def test_template_builtins_inside_a_lambda(source: str) -> None:
    # 0.2.57 read the generated code's own names from `globals()` inside a lambda, as it must
    # inside a class (where Python mangles them): but `__rs_tpl` is a parameter of a compiled
    # expression, so a template builtin read in a lambda raised `KeyError: '__rs_tpl'`.
    assert chameleon_rs.PageTemplate(source)() == chameleon.PageTemplate(source)()


@pytest.mark.parametrize(
    ('source', 'data'),
    [
        # The last operand of `and` / `or` is the result, never tested for truth.
        ('${fallback or report}', {'fallback': None}),
        ('${True and report}', {}),
        ('${fallback or 0 or report}', {'fallback': ''}),
        ('${report or fallback}', {'fallback': 1}),
        # Zero-argument `locals()` and `vars()` read the render function's frame.
        ('${locals().get("econtext") is econtext}', {}),
        ('${vars().get("econtext") is econtext}', {}),
        # A dict display evaluates its keys and values before it is built.
        ("${{'a': 1, []: 2, 'c': later}}", {}),
        ('${{[]: 2}}', {}),
        ('${{' + ', '.join(f'{i}: {i}' for i in range(15)) + ', []: later}}', {}),
        ('${{' + ', '.join(f'{i}: {i}' for i in range(16)) + ', []: later}}', {}),
        ('${{[]: 1, ' + ', '.join(f'{i}: {i}' for i in range(17)) + ', 99: later}}', {}),
        ('${{' + ', '.join(f'{i}: {i}' for i in range(17)) + ', []: 1, 99: later}}', {}),
        ('${sorted({' + ', '.join(f'{i}: {i}' for i in range(40)) + '})[-1]}', {}),
    ],
)
def test_native_expressions_evaluate_as_python_does(source: str, data: dict[str, Any]) -> None:
    class Report:
        def __bool__(self) -> bool:
            raise ValueError('truth value is ambiguous')

        def __str__(self) -> str:
            return 'report'

    def outcome(engine: Any) -> str:
        try:
            return engine.PageTemplate(source)(report=Report(), **data)  # type: ignore[no-any-return]
        except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
            return f'{type(e).__name__}: {str(e).splitlines()[0]}'

    assert outcome(chameleon_rs) == outcome(chameleon)


@pytest.mark.parametrize('source', ['${dict(x=1, x=2)}', '<i tal:content="f(a=1, b=2, a=3)"/>'])
def test_a_repeated_keyword_argument_is_a_syntax_error(source: str) -> None:
    # The compiler's error, not the parser's: `ast.parse` accepts the call, and the native
    # evaluator took the last value.
    for engine in (chameleon, chameleon_rs):
        with pytest.raises(SyntaxError, match='keyword argument repeated'):
            engine.PageTemplate(source)


def test_non_ascii_text_keeps_the_candidate_bound() -> None:
    # One non-ASCII character in a text took the bound away for all of it: every `${` then
    # tried each candidate up to the text's end (800 fields: 0.8 s to compile, against 2 ms).
    import time

    def compile_time(separator: str) -> float:
        body = separator.join(['${x}'] * 800)
        best = float('inf')
        for _ in range(3):
            start = time.perf_counter()
            template = chameleon_rs.PageTemplate(body)
            best = min(best, time.perf_counter() - start)
        assert template(x='X') == separator.join(['X'] * 800)
        return best

    assert compile_time(' \u00b7 ') < 20 * compile_time(' - ')


@pytest.mark.parametrize(
    'source',
    [
        "<p>é ${'}'} ü ${a}</p>",
        "<p>${a} · ${é'x'} · ${a}</p>",
        '<p title="é ${a} } ü ${b}">${a}é}</p>',
        '<p>${a + é} ${a}</p>',
        "<p>${'é' + a} · ${ {'ü': a}['ü'] } }</p>",
    ],
)
def test_interpolations_in_non_ascii_text_end_where_chameleons_do(source: str) -> None:
    def outcome(engine: Any) -> str:
        try:
            return engine.PageTemplate(source)(a='A', b='B')  # type: ignore[no-any-return]
        except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
            return f'{type(e).__name__}: {str(e).splitlines()[0]}'

    assert outcome(chameleon_rs) == outcome(chameleon)


def test_a_parameter_named_econtext_does_not_hide_the_scope() -> None:
    source = '<?python\ndef f(econtext):\n    return (a, econtext)\nv = f(1)\n?>${v}'
    assert chameleon_rs.PageTemplate(source)(a=100) == chameleon.PageTemplate(source)(a=100)


def test_nonlocal_in_a_code_block() -> None:
    # `SyntaxError: no binding for nonlocal 'n' found` in Chameleon, which stores `n = 0` in
    # the scope: here the name is the enclosing function's local, and the scope's `n` is left.
    source = (
        '<?python\ndef counter():\n    n = 0\n    def inc():\n        nonlocal n\n        n += 1\n'
        '        return n\n    return inc\ninc = counter()\n?>${inc()} ${inc()} ${n}'
    )
    assert chameleon_rs.PageTemplate(source)(n='data') == '1 2 data'
    with pytest.raises(SyntaxError):
        chameleon.PageTemplate(source)
