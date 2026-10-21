"""Phase 5, expressions: every TALES expression type and pipe, name resolution, non-strict
mode, custom expression types, `load:`, and free variables, compared with the reference
Chameleon (same value or same exception type and arguments)."""

from __future__ import annotations

import ast
import functools
import itertools
import os
from pathlib import Path
from typing import Any

import chameleon
import chameleon.astutil
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
        return self.missing  # type: ignore[attr-defined, no-any-return]

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
    '<input tal:attributes="checked {e}" checked/>',
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
        return (type(e).__name__, e.offset, str(e.token), tuple(e.location), e.args[0])


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
        call = ast.Call(func=func, args=[ast.Constant(self.expression)], keywords=[])
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
    with pytest.raises(LookupError, match="Unknown expression type: 'load'"):
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
