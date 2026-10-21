"""Render-time errors (Phase 6a): `tal:on-error`, Chameleon's error tracking
(`rcontext['__error__']`) and the formatted `RenderError`, global `tal:repeat`, and
`<?python ?>` code blocks, each rendered by both engines and compared: the output, or the
exception's type and its whole message (object addresses and generated ids normalized).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import chameleon
import chameleon.zpt.template
import chameleon_rs
import chameleon_rs.exc
import pytest

ADDRESS = re.compile(r'0x[0-9a-fA-F]+')
BACKUP = re.compile(r'__backup_(\w+?)_\d+')


def canon(text: str) -> str:
    text = ADDRESS.sub('0x?', text)
    text = BACKUP.sub(r'__backup_\1_?', text)
    return text.replace('chameleon_rs.', 'chameleon.')


def outcome(engine: Any, source: str, template_class: Any = None, **kw: Any) -> tuple[Any, ...]:
    cls = template_class or engine.PageTemplate
    try:
        return ('ok', canon(cls(source)(**kw)))
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return ('raise', type(e).__name__, canon(str(e)))


def same(source: str, **kw: Any) -> tuple[Any, ...]:
    want = outcome(chameleon, source, **kw)
    got = outcome(chameleon_rs, source, **kw)
    assert got == want, source
    return got


def test_on_error_leaks_a_failed_bodys_defines_and_loop_names() -> None:
    # Chameleon's generated code has no try/finally: dropping, not restoring (plan.md).
    source = '<div tal:on-error="x"><p tal:define="x 2"><b tal:repeat="i [7]">${i}${1/0}</b></p></div>|${x}|${i}'
    assert same(source) == ('ok', '<div>2</div>|2|7')


ON_ERROR = [
    '<div tal:on-error="string:ERR"><span tal:define="v 1">${1/0}</span></div>|${v}',
    '<p tal:on-error="error">${1/0}</p>|${error.type.__name__}|${error.value}|${error.lineno}|${error.offset}',
    '<p tal:on-error="structure string:&lt;b&gt;"><i>${1/0}</i></p>',
    '<p tal:on-error="nothing"><i>${1/0}</i></p>',
    '<p tal:on-error="default"><i>${1/0}</i></p>',
    '<p tal:on-error="1/0"><i>${1/0}</i></p>',
    '<p tal:on-error="string:a" tal:omit-tag="">x${1/0}</p>',
    '<p tal:on-error="string:a" tal:omit-tag="True">x${1/0}</p>',
    '<p tal:on-error="string:a" tal:omit-tag="False">x${1/0}</p>',
    '<p tal:on-error="string:a" tal:replace="1/0">x</p>',
    '<p tal:on-error="string:a" tal:content="1/0" />',
    '<p tal:on-error="string:a" tal:content="1/0">x</p>',
    '<tal:p tal:on-error="string:a">x${1/0}</tal:p>',
    '<tal:p on-error="string:a">x${1/0}</tal:p>',
    '<p tal:on-error="string:outer"><b tal:on-error="1/0">${1/0}</b></p>',
    '<p tal:on-error="string:a" tal:repeat="i [1,2]">${i}${1/(i-2)}</p>',
    '<p tal:repeat="i [1, 0, 2]" tal:on-error="string:E${i}">${1/i}</p>',
    '<p tal:on-error="string:a" tal:define="global g 5">${1/0}</p>${g}',
    '<p tal:on-error="string:a" tal:condition="1/0">x</p>',
    '<p tal:on-error="string:A">a<b tal:on-error="string:x">B</b>C${1/0}</p>',
    '<p tal:on-error="string:E${error.lineno},${error.offset}">a<b tal:on-error="string:x">B</b>C${y}</p>',
    '<i tal:on-error="string:1">${a}</i><i tal:on-error="string:2">ok</i><i tal:on-error="string:3">${c}</i>',
    '<p tal:define="x 1"/><p tal:on-error="string:E" tal:define="x 2">${1/0}</p>${x}',
    '<p class="c" id="i" tal:on-error="string:E">${1/0}</p>',
    '<p class="c" tal:attributes="title 1/0" tal:on-error="string:E">x</p>',
    '<p tal:attributes="title 1/0" tal:on-error="string:E" a="1">x</p>',
    '<br class="c" tal:on-error="string:E" title="${1/0}" />',
    '<p tal:on-error="string:E" tal:switch="1"><b tal:case="1">${1/0}</b></p>|${1}',
    '<p tal:on-error="string:E" tal:attributes="title 1/0" tal:omit-tag="">x</p>',
    '<p tal:on-error="python: str(error.type)">${undefined}</p>',
    '<p tal:on-error="error.value">${{}["k"]}</p>',
    '<p tal:on-error="text string:&lt;b&gt;">${1/0}</p>',
    '<div tal:on-error="string:E"><input checked="${1/0}" /></div>',
    '<div tal:on-error="string:E"><p tal:repeat="i 5">${i}</p></div>',
    '<div tal:on-error="string:E${repeat.i.index}"><p tal:repeat="i [1, 2]">${i}${1 / (i - 2)}</p></div>',
    '<div tal:on-error="string:E"><?python 1/0 ?></div>',
    '<div tal:on-error="string:E" tal:define="r rcontext"><p>${1/0}</p></div>${r}',
]


@pytest.mark.parametrize('source', ON_ERROR)
def test_on_error(source: str) -> None:
    same(source)


def test_on_error_catches_exceptions_only() -> None:
    class Stop(BaseException):
        pass

    def stop() -> None:
        raise Stop

    for engine in (chameleon, chameleon_rs):
        with pytest.raises(Stop):
            engine.PageTemplate('<p tal:on-error="string:E">${stop()}</p>')(stop=stop)


def test_on_error_handler_is_called_with_the_exception() -> None:
    for engine in (chameleon, chameleon_rs):
        seen: list[BaseException] = []
        template = engine.PageTemplate('<tal:b on-error="string:error">${test}</tal:b>', on_error_handler=seen.append)
        assert template() == 'error'
        assert [type(e).__name__ for e in seen] == ['NameError']
        # The scope's `__on_error_handler` is the one used.
        seen2: list[BaseException] = []
        assert engine.PageTemplate('<b tal:on-error="string:E">${1/0}</b>')(__on_error_handler=seen2.append)
        assert [type(e).__name__ for e in seen2] == ['ZeroDivisionError']


def test_a_failing_error_handler_propagates() -> None:
    def handler(e: BaseException) -> None:
        raise KeyError('from handler')

    for engine in (chameleon, chameleon_rs):
        with pytest.raises(KeyError, match='from handler'):
            engine.PageTemplate('<b tal:on-error="string:E">${1/0}</b>', on_error_handler=handler)()


ERRORS = [
    '<p tal:content="not: 1/0">x</p>',
    '<p tal:content="exists: 1/0">x</p>',
    '<p tal:content="exists: not: 1/0">x</p>',
    '<p tal:content="string:a${1/0}b">x</p>',
    '<p tal:content="string:a${x}b${1/0}">x</p>',
    '<p tal:content="x | 1/0">x</p>',
    '<p tal:content="1/0 | x">x</p>',
    '<p tal:content="x | not: 1/0">x</p>',
    '<p tal:content="structure: 1/0">x</p>',
    '<p>${x}${1/0}</p>',
    '<p>${1}${1/0}</p>',
    '<p title="${x} ${1/0}">a</p>',
    '<p tal:content=" 1/0 ">x</p>',
    '<p tal:define="a 1; b 1/0">x</p>',
    '<p tal:repeat="i 1/0">x</p>',
    '<p tal:repeat="i 5">x</p>',
    '<p tal:repeat="i [1,2]">${i}</p><p tal:define="x 1/0"/>',
    '<p tal:repeat="(a, b) [1]">x</p>',
    '<p tal:attributes="title 1/0">x</p>',
    '<p tal:attributes="d">x</p>',
    '<p tal:condition="1/0">x</p>',
    '<p tal:switch="1/0"><b tal:case="1">x</b></p>',
    '<p tal:omit-tag="1/0">x</p>',
    '<p tal:replace="1/0">x</p>',
    '<p>é\nü\n  ${1/0}</p>',
    '<p tal:define="d {}">${d.missing}</p>',
    '<p tal:content="python: [x for x in undefined_name]">x</p>',
    '<p tal:define="global g 2">${g}${1/0}</p>',
    '<p tal:define="v 1">${v}${repeat.missing}</p>',
    '<p>${b"\\xff"}</p>',
    '<p tal:content="b\'\\xff\'"/>',
    '<p tal:content="import:no.such.module">x</p>',
    '${"x" * 70}${1/0}',
    # A position-less expression (a processing instruction's) sets no token: the previous
    # one counts. Two of them crash Chameleon's compiler.
    '<p tal:define="a 1">${a}</p><?php ${1/0} ?>',
    '<p>${1}</p><?php ${2} ${1/0} ?>',
    '<?php ${} ${2} ?>',
    '<?php ${2} $${3} ?>',
    # Nested tokens are those of the site's latest evaluation.
    '<p tal:repeat="i [0, 1]" tal:content="i | not: 1/0">${1/i}</p>',
    '<b tal:repeat="i [0, 1]"><p tal:content="python: i or None | not: 1/0">x</p>${1/i}</b>',
    '<p tal:repeat="i [1, 0]" tal:content="string:${i}${1/i}"/>',
    '<p tal:repeat="i [1, 0]" tal:on-error="string:E${error.offset}"><i tal:content="not: i"/>${1/i}</p>',
]


@pytest.mark.parametrize('source', ERRORS)
def test_render_errors_are_formatted_like_chameleons(source: str) -> None:
    same(source)


def test_render_error_type_and_bases() -> None:
    for engine in (chameleon, chameleon_rs):
        with pytest.raises(ZeroDivisionError) as info:
            engine.PageTemplate('<p tal:define="d 1">${1/0}</p>')(x=1)
        exc = info.value
        assert type(exc).__name__ == 'ZeroDivisionError'
        assert type(exc).__bases__ == (ZeroDivisionError, engine.exc.RenderError)
        assert exc.args == ('division by zero',)
        assert isinstance(exc.__context__, ZeroDivisionError)
        assert 'NameError' not in str(exc)


def test_render_error_argument_values() -> None:
    kw = {'s': 'x' * 80 + '\n', 'n': 5, 'f': 1.5, 'd': {'a': 1}, 'o': object(), 'fn': len}
    for source in ('<p tal:define="a s">${1/0}</p>', '<p tal:define="global g 1">${undefined}</p>'):
        assert outcome(chameleon_rs, source, **kw) == outcome(chameleon, source, **kw)


def test_a_custom_value_repr_is_used() -> None:
    class Template(chameleon_rs.PageTemplate):
        value_repr = staticmethod(lambda value: '<hidden>')

    with pytest.raises(NameError) as info:
        Template('${x}')(secret='s')
    assert 'secret: <hidden>' in str(info.value)


def test_uncopyable_exceptions_are_raised_unformatted() -> None:
    class Difficult(Exception):
        def __init__(self) -> None:
            super().__init__()

    def raise_it() -> None:
        raise Difficult

    for engine in (chameleon, chameleon_rs):
        with pytest.raises(Difficult) as info:
            engine.PageTemplate('${f()}')(f=raise_it)
        assert '- Expression:' in str(info.value)


def test_errors_are_recorded_in_rcontext() -> None:
    kept: list[dict[str, Any]] = []

    def keep(r: dict[str, Any]) -> str:
        kept.append(r)
        return ''

    for engine in (chameleon, chameleon_rs):
        kept.clear()
        with pytest.raises(ZeroDivisionError) as info:
            engine.PageTemplate('${keep(rcontext)}\n  <p tal:content="1/0"/>')(keep=keep)
        [(token, line, column, filename, exc)] = kept[0]['__error__']
        assert (str(token), line, column, filename) == ('1/0', 2, 18, '<string>')
        assert exc is info.value.__context__


def test_formatted_errors_of_nested_renders_are_extended() -> None:
    for engine in (chameleon, chameleon_rs):
        inner = engine.PageTemplate('<b>${1/0}</b>')
        outer = engine.PageTemplate('<p>\n${inner()}</p>')
        with pytest.raises(ZeroDivisionError) as info:
            outer(inner=inner)
        text = canon(str(info.value))
        assert text.count('- Expression:') == 2
        assert '"inner()"' in text
        assert '"1/0"' in text


def test_file_templates_show_the_source_line(tmp_path: Path) -> None:
    path = tmp_path / 'page.pt'
    path.write_text(
        '<html>\n  <body>\n    <p class="x" tal:content="items[3].title | missing">x</p>\n  </body>\n</html>\n'
    )
    want = outcome(chameleon, str(path), template_class=chameleon.zpt.template.PageTemplateFile, items=[])
    got = outcome(chameleon_rs, str(path), template_class=chameleon_rs.PageTemplateFile, items=[])
    assert got == want
    assert ' - Source:     ' in got[2]


def test_recursion_errors_are_not_formatted(tmp_path: Path) -> None:
    (tmp_path / 'me.pt').write_text('<b>${render_partial("me.pt")}</b>')
    with pytest.raises(RecursionError) as info:
        chameleon_rs.PageTemplateFile(str(tmp_path / 'me.pt'))()
    assert '- Expression:' not in str(info.value)
    # An on-error handler can catch it.
    (tmp_path / 'page.pt').write_text('<p tal:on-error="string:deep">${render_partial("me.pt")}</p>')
    assert chameleon_rs.PageTemplateFile(str(tmp_path / 'page.pt'))() == '<p>deep</p>'


GLOBAL_REPEAT = [
    '<p tal:repeat="global i [1,2]">${i}</p>${i}',
    '<p tal:repeat="global i []">${i}</p>${i}',
    '<div tal:on-error="string:E${rcontext}"><p tal:repeat="global i [1,2]">${i}</p></div>${i}|${rcontext}',
    '<div tal:on-error="string:E"><p tal:repeat="global (a, b) [(1, 2), (3, 4)]">${a}</p></div>${a}${b}|${rcontext}',
    '<div tal:on-error="string:E"><p tal:repeat="global i iter([1])">${i}</p></div>${repeat.i.length}',
    '<div tal:on-error="string:E"><p tal:repeat="global (a, b) [iter((1, 2))]">${a}${b}</p></div>${rcontext}',
]


@pytest.mark.parametrize('source', GLOBAL_REPEAT)
def test_global_repeat(source: str) -> None:
    same(source)


CODE_BLOCKS = [
    '<?python x = 1 ?>${x}',
    '<?python def f(i): return i + 1 ?>${f(2)}|${i | 0}',
    '<?python def f(i): return i + 1 ?><p tal:repeat="i [1]">${i | 0}</p>',
    '<?python import os ?>${os.sep}',
    '<?python import os.path ?>${os.path.sep}',
    '<?python import os.path as p ?>${p.sep}',
    '<?python x = ?>',
    '<?python 1/0 ?>',
    '<?python\n    a = 1\n  b = 2 ?>',
    '<p tal:condition="False"><?python def g(): return 1 ?></p>${g()}',
    '<p tal:condition="False"><?python import os ?></p><?python print(os) ?>',
    '<?python y = [a for a in (1, 2)] ?>${y}|${a}',
    '<?python def f(): return foo ?><p tal:define="foo 3">${f()}</p>',
    '<?python\n  for q in range(2):\n    pass\n?>${q}',
    '<?python translate = None ?>${1}',
    '<?python def f(x=lambda z: z): return x(3) ?>${f()}${z | 9}',
    '<?python class C: pass ?>${C | 7}',
    '<p tal:define="x 5"><?python x = x + 1 ?>${x}</p>${x | 0}',
    '<?python def f(i): return i ?>${f(1)}<?python i = 4 ?>${i}${econtext["i"]}',
    '<?python def f(i): return i ?><p tal:define="i 3">${i}</p>',
    '<?python def f(len): return 1 ?>${len("ab")}',
    '<?python def f(): return 1 ?><p title="${f()}" tal:attributes="class f()">${f() + 1}</p>',
    '<?python def f(): return 1 ?><p tal:repeat="x [f()]" tal:content="x">${f()}</p>',
    '<?python def f(): return rcontext ?>${f()}',
    '<?python def f(): return econtext["v"] ?><p tal:define="v 7">${f()}</p>',
    '<div>\n  <?python numbers = map(str, range(1, 10)) ?>\n  ${", ".join(numbers)}\n</div>',
    '<?python import sys ?><?python x = sys.maxsize > 0 ?>${x}',
    '<?python g = (lambda: undefined_thing)() ?>',
    '<p tal:on-error="string:E${error.lineno}:${error.offset}">\n<?python 1/0 ?></p>',
]


@pytest.mark.parametrize('source', CODE_BLOCKS)
def test_code_blocks(source: str) -> None:
    same(source)


def test_code_block_namespaces_are_per_render() -> None:
    template = chameleon_rs.PageTemplate('${g() | 0}<?python def g(): return 1 ?>')
    reference = chameleon.PageTemplate('${g() | 0}<?python def g(): return 1 ?>')
    for _ in range(2):
        assert outcome(chameleon_rs, '') and template() == reference()


def test_errors_inside_partials_are_recorded_in_their_own_render(tmp_path: Path) -> None:
    # As chameleon_partials' nested `render`: the partial's frames record into its own
    # `rcontext`; the page's gets only the page's frames (none here: the error was caught).
    kept: list[dict[str, Any]] = []
    (tmp_path / 'inner.pt').write_text('<i>\n${1/0}</i>')
    (tmp_path / 'page.pt').write_text('<p tal:on-error="string:E">${render_partial("inner.pt")}</p>${keep(rcontext)}')

    def keep(r: dict[str, Any]) -> str:
        kept.append(r)
        return ''

    assert chameleon_rs.PageTemplateFile(str(tmp_path / 'page.pt'))(keep=keep) == '<p>E</p>'
    assert '__error__' not in kept[0]


def test_deferred_syntax_errors_report_their_own_token() -> None:
    # Non-strict mode: `TokenRef(exc.token)` precedes the raise.
    for engine in (chameleon, chameleon_rs):
        with pytest.raises(engine.exc.ExpressionError):
            engine.PageTemplate('<p>${1 +}</p>', strict=False)()
    source = '<p tal:on-error="string:E${error.lineno}:${error.offset}">\n  ${a +}</p>'
    want = chameleon.PageTemplate(source, strict=False)()
    assert chameleon_rs.PageTemplate(source, strict=False)() == want == '<p>E2:4</p>'


def test_caught_errors_cost_does_not_grow_with_the_template() -> None:
    # The error path resolves its token from a table built at compile time; it once rebuilt
    # the line index and scanned the source per caught error (287x Chameleon on 421 KB).
    import timeit

    body = (
        '<p>' + 'é' * 10 + '\n' + 'x' * 400_000 + '\n</p>'
        '<i tal:repeat="i items"><b tal:on-error="string:E">${1/0}</b></i>' + '<p>q</p>\n' * 200
    )
    items = range(2000)
    times = {}
    for engine in (chameleon, chameleon_rs):
        template = engine.PageTemplate(body)
        template(items=[1])
        times[engine.__name__] = min(timeit.repeat(lambda t=template: t(items=items), number=1, repeat=3))
    # Generous: we measure about 0.6x Chameleon; a per-error scan of the source is 100x+.
    assert times['chameleon_rs'] < 5 * times['chameleon'] + 0.005, times
