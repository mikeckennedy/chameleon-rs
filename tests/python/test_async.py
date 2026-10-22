"""`render_async` (Phase 11, D9).

- Differential: with no awaitable value, `render_async` gives exactly what `render` and
  Chameleon give, for every upstream fixture and for the render fuzzers' generated templates
  (plain, macros and i18n, partials).
- Awaitables at every value position (content, attributes, conditions, defines, repeat
  sources, switch, macro names, slot fills, partial arguments, i18n names and attributes,
  on-error), generated: the async render with `aw(v)` a coroutine equals Chameleon's render
  with `aw(v) = v`.
- Suspension inside macros, slots, native partials and on-error; errors of awaited values
  caught by `tal:on-error`; the coroutine protocol (`send`, `throw`, `close`, reuse,
  re-entry); cancellation mid-render; many renders on one loop; no reference cycles left by
  suspended or cancelled renders; the event loop keeps running during a long render.
- Opt-in concurrency: `@concurrent` values and partial fan-out.
"""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import time
import types
import warnings
import weakref
from pathlib import Path
from typing import Any

import chameleon
import chameleon_rs
import chameleon_rs.exc
import pytest
import test_fixtures as fixtures_mod
import test_render_fuzz as fuzz
from chameleon_rs import PageTemplate, PageTemplateFile, PageTextTemplate, concurrent
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

EXAMPLES = fuzz.EXAMPLES
LOOP = asyncio.new_event_loop()


def run(awaitable: Any) -> Any:
    return LOOP.run_until_complete(awaitable)


async def aw(value: Any) -> Any:
    await asyncio.sleep(0)
    return value


async def aw_raise(exc: BaseException) -> Any:
    await asyncio.sleep(0)
    raise exc


def same_outcome(want: Any, got: Any, body: str) -> None:
    """The render fuzzers' comparison: the same output, or the same exception type and
    formatted message."""
    if isinstance(want, Exception):
        assert isinstance(got, Exception), (body, want, got)
        names = {c.__name__ for c in type(want).__mro__}
        assert type(got).__name__ in names, (body, want, got)
        if not isinstance(want, (chameleon.exc.TemplateError, RecursionError)) and 'recursion' not in str(want):
            assert fuzz.canon(str(got)) == fuzz.canon(str(want)), body
        return
    assert not isinstance(got, Exception), (body, want, got)
    assert fuzz.canon(got) == fuzz.canon(want), body


# -- No awaitable values: render_async == render == Chameleon -------------------------------


def render_fixture_async(filename: str, language: str | None) -> Any:
    """`test_fixtures.render` for chameleon_rs, through `render_async`."""
    path = os.path.join(fixtures_mod.INPUTS, filename)
    text = filename.endswith('.txt')
    implicit = 'implicit-i18n' in filename
    options: dict[str, Any] = {
        'strict': False,
        'implicit_i18n_translate': implicit,
        'implicit_i18n_attributes': ('alt', 'title') if implicit else (),
        'enable_data_attributes': 'data-attributes' in filename,
        'keep_source': True,
    }
    loader = chameleon_rs.loader.TemplateLoader(fixtures_mod.INPUTS)
    factory = chameleon_rs.PageTextTemplateFile if text else chameleon_rs.PageTemplateFile
    template = factory(path, **options)
    params: dict[str, Any] = {}
    if not text:
        params = {
            'literal': fixtures_mod.Literal('<div>Hello world!</div>'),
            'content': '<div>Hello world!</div>',
            'message': fixtures_mod.upstream.Message(),
            'load': loader.bind(chameleon_rs.PageTemplateFile),
        }
    params.update(translate=fixtures_mod.upstream.TestZopePageTemplates.translate, target_language=language)
    return run(template.render_async(**params))


@pytest.mark.parametrize(('filename', 'language'), fixtures_mod.fixtures(), ids=lambda v: str(v))
def test_fixture_renders_async_like_chameleon(filename: str, language: str | None) -> None:
    want = fixtures_mod.render('chameleon', filename, language)
    got = render_fixture_async(filename, language)
    assert type(got) is type(want)
    assert got == want


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=st.lists(fuzz.node(2), min_size=1, max_size=3).map(''.join))
def test_generated_templates_render_async_like_chameleon(body: str) -> None:
    def sync() -> Any:
        try:
            return chameleon.PageTemplate(body)(**fuzz.VARIABLES)
        except Exception as e:  # noqa: BLE001 - comparing exception types across engines
            return e

    async def asynchronous() -> Any:
        try:
            return await chameleon_rs.PageTemplate(body).render_async(**fuzz.VARIABLES)
        except Exception as e:  # noqa: BLE001
            return e

    same_outcome(sync(), run(asynchronous()), body)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    body=st.lists(fuzz.i18n_node(2), min_size=1, max_size=3).map(''.join),
    implicit=st.booleans(),
    language=st.sampled_from([None, 'de']),
    mode=st.sampled_from(['show', 'empty', 'short', 'non-ascii']),
)
def test_generated_macro_and_i18n_templates_render_async_like_chameleon(
    body: str, implicit: bool, language: str | None, mode: str
) -> None:
    options: dict[str, Any] = {
        'implicit_i18n_translate': implicit,
        'implicit_i18n_attributes': {'title', 'a'} if implicit else (),
    }
    kw: dict[str, Any] = dict(fuzz.VARIABLES, translate=fuzz.TRANSLATIONS[mode], target_language=language)

    def sync() -> Any:
        try:
            return chameleon.PageTemplate(body, **options)(**kw)
        except Exception as e:  # noqa: BLE001
            return e

    async def asynchronous() -> Any:
        try:
            return await chameleon_rs.PageTemplate(body, **options).render_async(**kw)
        except Exception as e:  # noqa: BLE001
            return e

    same_outcome(sync(), run(asynchronous()), body)


_partial_count = iter(range(10**9))


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=st.lists(fuzz.node(2), min_size=1, max_size=3).map(''.join), in_loop=st.booleans())
def test_generated_partials_render_async_like_chameleon_partials(body: str, in_loop: bool) -> None:
    import chameleon_partials
    import chameleon_rs._settings

    name = f'a{next(_partial_count)}.pt'
    with open(os.path.join(fuzz.PARTIALS_DIR, name), 'w') as f:
        f.write(body)
    call = '${render_partial("%s", %s)}' % (name, ', '.join(f'{k}={k}' for k in fuzz.VARIABLES))  # noqa: UP031
    page = f'<b tal:repeat="r [1, 2]">{call}</b>' if in_loop else f'<i>{call}</i>'
    chameleon_partials.register_extensions(fuzz.PARTIALS_DIR, cache_init=False)
    chameleon_rs.configure(template_folder=fuzz.PARTIALS_DIR)

    def sync() -> Any:
        try:
            return chameleon.PageTemplate(page)(render_partial=chameleon_partials.render_partial, **fuzz.VARIABLES)
        except Exception as e:  # noqa: BLE001
            return e

    async def asynchronous() -> Any:
        try:
            return await chameleon_rs.PageTemplate(page).render_async(
                render_partial=chameleon_rs.render_partial, **fuzz.VARIABLES
            )
        except Exception as e:  # noqa: BLE001
            return e

    try:
        same_outcome(sync(), run(asynchronous()), body)
    finally:
        chameleon_rs._settings.clear()


# -- Awaitables at every value position, generated --------------------------------------

# Plain Python expressions (a final value can be wrapped in `aw(...)`).
PY_EXPR = [
    'x',
    'z',
    'n',
    's',
    'e',
    'h',
    'xs',
    'd',
    't',
    'f',
    'default',
    'x + 1',
    'not x',
    'xs[0]',
    'o.k',
    'o.p',
    'o.m(1)',
    'o.html',
    'msg',
    's.upper()',
    "d.get('a')",
]


@st.composite
def value(draw: Any) -> str:
    """An expression whose final value may be awaitable: `aw(E)`, an awaited error, or `E`."""
    kind = draw(st.sampled_from(['aw', 'aw', 'plain', 'boom']))
    if kind == 'boom':
        return 'boom()'
    expr = draw(st.sampled_from(PY_EXPR))
    return f'aw({expr})' if kind == 'aw' else expr


@st.composite
def aw_attributes(draw: Any) -> str:
    return '; '.join(
        f'{draw(st.sampled_from(["a", "b", "checked", "title"]))} {draw(value())}'
        for _ in range(draw(st.integers(1, 2)))
    )


AW_TAL: dict[str, Any] = {
    'tal:define': st.builds(lambda g, n, e: f'{g}{n} {e}', st.sampled_from(['', 'global ']), fuzz.NAME, value()),
    'tal:condition': value(),
    'tal:repeat': st.builds(
        lambda n, e: f'{n} {e}', fuzz.NAME, st.sampled_from(['aw(xs)', 'aw([1, 2])', 'xs', 'aw(s)', 'boom()'])
    ),
    'tal:content': st.builds(lambda k, e: k + e, st.sampled_from(['', 'structure ']), value()),
    'tal:replace': st.builds(lambda k, e: k + e, st.sampled_from(['', 'structure ']), value()),
    'tal:attributes': aw_attributes(),
    'tal:omit-tag': st.sampled_from(['', 'aw(x)', 'aw(z)', 'x']),
    'tal:switch': value(),
    'tal:case': value(),
    'a': st.builds(lambda e: '${%s}' % e, value()),  # noqa: UP031
    'title': st.builds(lambda e, f: 'p${%s}q${%s}' % (e, f), value(), value()),  # noqa: UP031
    'checked': st.builds(lambda e: '${%s}' % e, value()),  # noqa: UP031
}


@st.composite
def aw_element(draw: Any, depth: int) -> str:
    tag = draw(st.sampled_from(['p', 'tal:b', 'input']))
    names = draw(st.lists(st.sampled_from(sorted(AW_TAL)), max_size=3, unique=True))
    if 'tal:content' in names and 'tal:replace' in names:
        names.remove('tal:replace')
    if tag == 'tal:b':
        names = [n for n in names if n.startswith('tal:') and n != 'tal:attributes']
    attrs = ''.join(f' {name}="{draw(AW_TAL[name])}"' for name in names)
    if depth <= 0 or draw(st.booleans()):
        return f'<{tag}{attrs}>t</{tag}>'
    children = draw(st.lists(aw_node(depth - 1), max_size=3))
    return f'<{tag}{attrs}>{"".join(children)}</{tag}>'


@st.composite
def aw_guarded(draw: Any, depth: int) -> str:
    children = ''.join(draw(st.lists(aw_node(depth - 1), max_size=3))) if depth > 0 else '${%s}' % draw(value())  # noqa: UP031
    handler = draw(st.sampled_from(['string:E', 'error.type.__name__', 'aw(error.type.__name__)', 'aw(x)']))
    return f'<div tal:on-error="{handler}">{children}${{boom()}}</div>'


@st.composite
def aw_macro(draw: Any, depth: int) -> str:
    """A macro defined and used in the same template (by an awaited name), with a slot whose
    default and fill hold awaited values."""
    inner = ''.join(draw(st.lists(aw_node(depth - 1), max_size=2))) if depth > 0 else '${%s}' % draw(value())  # noqa: UP031
    fill = '${%s}' % draw(value())  # noqa: UP031
    name = draw(st.sampled_from(['aw(macros["m"])', 'macros["m"]', 'aw(template.macros["m"])']))
    return (
        f'<div metal:define-macro="m">{inner}<i metal:define-slot="s">${{{draw(value())}}}</i></div>'
        f'<p metal:use-macro="{name}"><b metal:fill-slot="s">{fill}</b></p>'
    )


@st.composite
def aw_i18n(draw: Any, depth: int) -> str:
    return (
        f'<p i18n:translate="">a <b i18n:name="who" tal:content="{draw(value())}">x</b> ${{{draw(value())}}}</p>'
        f'<a title="${{{draw(value())}}}" i18n:attributes="title">t</a>'
    )


def aw_node(depth: int) -> Any:
    text = st.builds(lambda e: '${%s}' % e, value())  # noqa: UP031
    return st.one_of(text, aw_element(depth), aw_guarded(depth), aw_macro(depth), aw_i18n(depth))


def _sync_aw(v: Any) -> Any:
    return v


def _sync_boom() -> Any:
    raise LookupError('boom')


def _async_boom() -> Any:
    return aw_raise(LookupError('boom'))


# The same names as the async helpers (a render error's message lists the arguments).
_sync_aw.__name__ = _sync_aw.__qualname__ = 'aw'
_sync_boom.__name__ = _sync_boom.__qualname__ = _async_boom.__name__ = _async_boom.__qualname__ = 'boom'


def _translate(msgid: Any, mapping: Any = None, default: Any = None, **kw: Any) -> Any:
    return f'[{msgid}:{sorted((mapping or {}).items())}:{default}]'


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=st.lists(aw_node(2), min_size=1, max_size=3).map(''.join))
def test_awaitables_at_every_position_render_like_chameleon(body: str) -> None:
    def sync() -> Any:
        try:
            return chameleon.PageTemplate(body)(**fuzz.VARIABLES, aw=_sync_aw, boom=_sync_boom, translate=_translate)
        except Exception as e:  # noqa: BLE001
            return e

    async def asynchronous() -> Any:
        try:
            return await chameleon_rs.PageTemplate(body).render_async(
                **fuzz.VARIABLES, aw=aw, boom=_async_boom, translate=_translate
            )
        except Exception as e:  # noqa: BLE001
            return e

    with _no_unawaited_coroutines():
        same_outcome(sync(), run(asynchronous()), body)


class _no_unawaited_coroutines:
    """Fails if a coroutine the render created was never awaited (collected unawaited)."""

    def __enter__(self) -> None:
        import warnings

        self._catch = warnings.catch_warnings(record=True)
        self._log = self._catch.__enter__()
        warnings.simplefilter('always')

    def __exit__(self, *exc: object) -> None:
        gc.collect()
        self._catch.__exit__(*exc)  # ty: ignore[invalid-argument-type]
        never = [w for w in self._log if 'never awaited' in str(w.message)]
        assert not never, never


# -- Positions and nesting, by hand ------------------------------------------------------


def test_value_positions() -> None:
    body = (
        '<div tal:define="x aw(1)" class="${aw(\'c\')}" title="a${aw(lt)}b">'
        '${x}${aw(lt)}<p tal:condition="aw(False)">no</p><p tal:condition="aw(True)">yes</p>'
        '<i tal:repeat="i aw([1, 2])">${aw(i)}</i><b tal:content="aw(\'c\')">d</b>'
        '<b tal:content="aw(default)">kept</b><s tal:replace="structure aw(lt + \'u/>\')"/>'
        '<a tal:attributes="href aw(\'/h\'); title aw(None)">l</a>'
        '<p tal:switch="aw(2)"><b tal:case="aw(1)">1</b><b tal:case="aw(2)">2</b></p>'
        '<input checked="${aw(True)}"/><input checked="${aw(False)}"/><p tal:omit-tag="aw(1)">o</p>'
        '</div>'
    )
    want = chameleon.PageTemplate(body)(aw=_sync_aw, lt='<')
    assert '<p>yes</p>' in want and '<b>kept</b>' in want and '<u/>' in want
    assert run(PageTemplate(body).render_async(aw=aw, lt='<')) == want


def collected(value: Any) -> Any:
    gc.collect()
    return value


def test_only_final_values_are_awaited() -> None:
    # `coro().real` is the coroutine's attribute (an AttributeError), as in a sync render.
    page = PageTemplate('<p tal:on-error="error.type.__name__">${aw(1).real}</p>${not aw(0)}')

    async def go() -> str:
        return await page.render_async(aw=aw)

    with pytest.warns(RuntimeWarning, match='never awaited'):
        out = collected(run(go()))
    assert out == '<p>AttributeError</p>False'


def test_sync_render_keeps_awaitables_as_values() -> None:
    page = PageTemplate('${v}')
    coro = aw(1)
    try:
        assert page.render(v=coro).startswith('&lt;coroutine object aw at')
    finally:
        coro.close()


def test_suspends_inside_macros_slots_partials_and_on_error(tmp_path: Path, configured: Path) -> None:
    (tmp_path / 'p.pt').write_text('<em tal:define="q x">${q}|${y}<i tal:repeat="k aw([1, 2])">${aw(k)}</i></em>')
    (tmp_path / 'layout.pt').write_text(
        '<html metal:define-macro="page"><b>${aw("L")}</b><div metal:define-slot="body">dflt</div>'
        '<p tal:on-error="string:caught ${error.type.__name__}">${boom()}</p></html>'
    )
    layout = PageTemplateFile(str(tmp_path / 'layout.pt'))
    page = PageTemplate(
        '<div metal:use-macro="aw(layout.macros[\'page\'])"><div metal:fill-slot="body">S${aw("fill")}'
        '${render_partial("p.pt", x=aw("px"), y=aw("py"), aw=aw)}</div></div>'
    )
    want = page.render(aw=_sync_aw, boom=lambda: {}['k'], layout=layout)
    assert want.startswith('<html><b>L</b><div>Sfill<em>px|py<i>1</i>')
    assert want.endswith('<p>caught KeyError</p></html>')
    assert run(page.render_async(aw=aw, boom=lambda: aw_raise(KeyError('k')), layout=layout)) == want


def test_error_from_awaited_value_is_formatted_like_a_sync_error() -> None:
    body = '<div>\n  <p tal:define="v boom()">${v}</p>\n</div>'

    def sync_boom() -> Any:
        raise KeyError('missing')

    with pytest.raises(KeyError) as sync_error:
        PageTemplate(body).render(boom=sync_boom)

    def async_boom() -> Any:
        return aw_raise(KeyError('missing'))

    async_boom.__name__ = async_boom.__qualname__ = 'sync_boom'
    with pytest.raises(KeyError) as async_error:
        run(PageTemplate(body).render_async(boom=async_boom))
    assert isinstance(async_error.value, chameleon_rs.exc.RenderError)
    assert fuzz.canon(str(async_error.value)) == fuzz.canon(str(sync_error.value))


def test_text_template_and_text_file(tmp_path: Path) -> None:
    assert run(PageTextTemplate('a ${aw(1)} b').render_async(aw=aw)) == 'a 1 b'
    (tmp_path / 't.txt').write_text('x${aw("é")}')
    out = run(chameleon_rs.PageTextTemplateFile(str(tmp_path / 't.txt')).render_async(aw=aw))
    assert out == 'xé'.encode()


def test_render_protocol_instance_renders_synchronously() -> None:
    template = chameleon_rs.template.BaseTemplate()
    template._cooked = True
    template._render = lambda stream, econtext, rcontext, **kw: stream.append(  # ty: ignore[invalid-assignment]
        str(econtext['x'])
    )
    assert run(template.render_async(x=5)) == '5'


def test_awaitable_kinds() -> None:
    class Awaitable:
        def __await__(self) -> Any:
            yield from asyncio.sleep(0).__await__()
            return 'custom'

    @types.coroutine
    def legacy() -> Any:
        yield from asyncio.sleep(0).__await__()
        return 'legacy'

    def generator() -> Any:
        yield 1

    async def main() -> str:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        loop.call_soon(future.set_result, 'future')
        task = asyncio.ensure_future(aw('task'))
        instance = types.SimpleNamespace(__await__=lambda: iter(()))  # instance attribute: not awaitable
        gen = generator()
        return await PageTemplate('${a} ${b} ${c} ${d} ${e} ${len(str(f))} ${g.__class__.__name__}').render_async(
            a=Awaitable(), b=legacy(), c=future, d=task, e=aw('coro'), f=instance, g=gen
        )

    assert run(main()).startswith('custom legacy future task coro ')
    assert run(main()).endswith(' generator')


def test_await_returning_a_non_iterator_is_a_type_error() -> None:
    class Bad:
        def __await__(self) -> Any:
            return 1

    page = PageTemplate('<p tal:on-error="error.value">${bad}</p>')
    assert run(page.render_async(bad=Bad())) == "<p>__await__() returned non-iterator of type 'int'</p>"


def test_async_iterables_in_repeat_are_not_supported() -> None:
    # Decided in Phase 11: as in a sync render, an async iterable is not iterable (collect it
    # in an async helper first); an awaitable that returns a list is fine.
    async def numbers() -> Any:
        for i in range(3):
            yield i

    async def collect(source: Any) -> list[Any]:
        return [x async for x in source]

    page = PageTemplate('<p tal:on-error="error.type.__name__"><i tal:repeat="x xs">${x}</i></p>')
    assert run(page.render_async(xs=numbers())) == '<p>TypeError</p>'
    assert run(page.render_async(xs=collect(numbers()))) == '<p><i>0</i>\n<i>1</i>\n<i>2</i></p>'


def test_infinite_macro_recursion_is_a_recursion_error() -> None:
    page = PageTemplate('<p metal:define-macro="m">${aw(1)}<i metal:use-macro="macros[\'m\']"/></p>')
    with pytest.raises(RecursionError):
        run(page.render_async(aw=aw))


def test_code_blocks_and_globals_across_suspensions() -> None:
    body = (
        '<?python a = 1 ?>${aw(a)}<?python b = a + 1 ?>${b}'
        '<div metal:define-macro="m" tal:define="global g aw(\'G\')">${aw(g)}</div>'
        "<p metal:use-macro=\"macros['m']\"/>${g}${aw(econtext.get('g'))}"
    )
    want = chameleon.PageTemplate(body)(aw=_sync_aw)
    assert run(PageTemplate(body).render_async(aw=aw)) == want


# -- The coroutine protocol --------------------------------------------------------------


def _awaitable(body: str, **kw: Any) -> Any:
    return chameleon_rs._native.render_page_async(PageTemplate(body), None, kw)


def test_protocol_send_none_and_stop_iteration() -> None:
    a = _awaitable('a${x}b', x=1)
    with pytest.raises(StopIteration) as stop:
        a.send(None)
    assert stop.value.value == 'a1b'
    with pytest.raises(RuntimeError, match='cannot reuse already awaited coroutine'):
        a.send(None)


def test_protocol_send_non_none_to_a_fresh_render() -> None:
    with pytest.raises(TypeError, match='just-started'):
        _awaitable('x').send(1)


def test_protocol_values_are_sent_through() -> None:
    class Waiter:
        def __await__(self) -> Any:
            got = yield 'question'
            return got * 2

    a = _awaitable('<p>${w}</p>', w=Waiter())
    assert a.send(None) == 'question'
    with pytest.raises(StopIteration) as stop:
        a.send(21)
    assert stop.value.value == '<p>42</p>'


def test_protocol_throw_reaches_on_error_and_the_render_continues() -> None:
    class Waiter:
        def __await__(self) -> Any:
            yield 'waiting'
            return 'never'

    a = _awaitable('<p tal:on-error="error.type.__name__">${w}</p>!', w=Waiter())
    a.send(None)
    with pytest.raises(StopIteration) as stop:
        a.throw(ValueError('thrown'))
    assert stop.value.value == '<p>ValueError</p>!'


def test_protocol_throw_cancellation_passes_on_error() -> None:
    a = _awaitable('<p tal:on-error="string:caught">${w}</p>', w=asyncio.sleep(0))
    a.send(None)
    with pytest.raises(asyncio.CancelledError):
        a.throw(asyncio.CancelledError())
    with pytest.raises(RuntimeError):
        a.send(None)


def test_protocol_throw_into_a_fresh_render_and_legacy_signature() -> None:
    with pytest.raises(KeyError):
        _awaitable('x').throw(KeyError)
    with pytest.raises(KeyError):
        _awaitable('x').throw(KeyError, KeyError('k'), None)
    with pytest.raises(TypeError):
        _awaitable('x').throw(1)


def test_protocol_close_closes_the_awaited_value() -> None:
    closed = []

    class Pending:
        def __await__(self) -> Any:
            yield 'pending'

    async def waiting() -> Any:
        try:
            await Pending()
        finally:
            closed.append(True)

    coro = waiting()
    a = _awaitable('${w}', w=coro)
    a.send(None)
    a.close()
    assert closed == [True]
    a.close()  # again: nothing
    with pytest.raises(RuntimeError):
        a.send(None)


def test_protocol_throw_generator_exit_closes_the_delegate() -> None:
    closed = []

    class Pending:
        def __await__(self) -> Any:
            yield 'pending'

    async def waiting() -> Any:
        try:
            await Pending()
        finally:
            closed.append(True)

    a = _awaitable('${w}', w=waiting())
    a.send(None)
    with pytest.raises(GeneratorExit):
        a.throw(GeneratorExit())
    assert closed == [True]


def test_protocol_re_entry_is_an_error() -> None:
    holder: list[Any] = []

    def reenter() -> str:
        try:
            holder[0].send(None)
        except ValueError as e:
            return str(e)
        return 'no error'

    a = _awaitable('${f()}', f=reenter)
    holder.append(a)
    with pytest.raises(StopIteration) as stop:
        a.send(None)
    assert stop.value.value == 'coroutine already executing'


def test_render_async_works_with_create_task_and_task_groups() -> None:
    page = PageTemplate('${aw(n)}')

    async def main() -> list[str]:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(page.render_async(aw=aw, n=i)) for i in range(5)]
        return [t.result() for t in tasks]

    assert run(main()) == ['0', '1', '2', '3', '4']


# -- Cancellation, isolation, GC ---------------------------------------------------------


def test_cancellation_mid_render_cleans_up(tmp_path: Path, configured: Path) -> None:
    (tmp_path / 'p.pt').write_text('<i tal:define="global g 1">${slow()}</i>')
    finally_ran = []

    async def slow() -> Any:
        try:
            await asyncio.sleep(10)
        finally:
            finally_ran.append(True)

    page = PageTemplate(
        '<div metal:define-macro="m"><p tal:on-error="string:caught">'
        '<b tal:repeat="i [1]">${render_partial("p.pt", slow=slow)}</b></p></div>'
        '<div metal:use-macro="macros[\'m\']"/>'
    )

    async def main() -> None:
        task = asyncio.ensure_future(page.render_async(slow=slow))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main())
    assert finally_ran == [True]


def test_many_renders_on_one_loop_keep_their_output_apart() -> None:
    page = PageTemplate(
        '<ul tal:define="me aw(n)"><li tal:repeat="i aw(range(n % 5))">${me}:${aw(i)}</li></ul>${render_partial}'
    )

    async def one(n: int) -> tuple[int, str]:
        return n, await page.render_async(aw=aw, n=n, render_partial='')

    async def main() -> list[tuple[int, str]]:
        return await asyncio.gather(*(one(n) for n in range(300)))

    for n, out in run(main()):
        assert out == page.render(aw=_sync_aw, n=n, render_partial=''), n
        assert out.count(f'<li>{n}:') == n % 5


@pytest.mark.filterwarnings('ignore:coroutine .* was never awaited:RuntimeWarning')
def test_suspended_and_cancelled_renders_leave_no_cycles() -> None:
    page = PageTemplate(
        '<div tal:define="global x aw(1)" metal:define-macro="m"><p tal:repeat="i [1, 2]">'
        '${aw(i)}${never()}</p></div><div metal:use-macro="macros[\'m\']"/>'
    )

    async def never() -> Any:
        await asyncio.sleep(10)

    async def main() -> None:
        tasks = [asyncio.ensure_future(page.render_async(aw=aw, never=never)) for _ in range(2000)]
        await asyncio.sleep(0.01)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    gc.collect()
    before = len(gc.get_objects())
    for _ in range(5):
        run(main())
    # Suspended renders dropped without being driven to the end, too.
    for _ in range(2000):
        a = _awaitable('${w}${w2}', w=aw(1), w2=asyncio.sleep(0))
        a.send(None)
        del a
    gc.collect()
    after = len(gc.get_objects())
    assert after - before < 500, (before, after)


def test_a_suspended_render_in_a_cycle_is_collected() -> None:
    class Holder:
        render: Any

    holder = Holder()
    coro = aw(1)
    a = _awaitable('${h}${w}', h=holder, w=coro)
    holder.render = a  # the render holds the holder (its scope), the holder holds the render
    ref = weakref.ref(holder)
    a.send(None)
    del a, holder, coro
    gc.collect()
    assert ref() is None


# -- The event loop keeps running --------------------------------------------------------


def test_event_loop_keeps_running_during_an_async_render() -> None:
    rows = PageTemplate('<p tal:repeat="i range(200)">${aw(i)} ${i * 2}</p>')

    async def main() -> float:
        worst = 0.0
        done = False

        async def ticker() -> None:
            nonlocal worst
            last = time.perf_counter()
            while not done:
                await asyncio.sleep(0)
                now = time.perf_counter()
                worst = max(worst, now - last)
                last = now

        tick = asyncio.ensure_future(ticker())
        await asyncio.sleep(0)
        await rows.render_async(aw=aw)
        done = True
        await tick
        return worst

    # Each step between two awaits renders one row: the loop never waits long. (The bound is
    # loose for shared CI machines; bench/bench_async.py measures it.)
    assert run(main()) < 0.05


# -- Opt-in concurrency ------------------------------------------------------------------


@concurrent
async def cslow(value: Any, delay: float = 0.05) -> Any:
    await asyncio.sleep(delay)
    return value


def test_concurrent_values_run_at_the_same_time() -> None:
    page = PageTemplate(
        '<p tal:repeat="i range(6)">${cslow(lt + str(i))} a${cslow(i)}b</p><i title="x${cslow(amp)}">${cslow(1)}</i>'
    )
    start = time.perf_counter()
    out = run(page.render_async(cslow=cslow, lt='<', amp='&'))
    elapsed = time.perf_counter() - start
    assert out == page.render(cslow=lambda v: v, lt='<', amp='&')
    assert elapsed < 0.04 * 14 / 2, elapsed  # 14 calls of 50 ms: well under half of 0.7 s


def test_concurrent_values_are_awaited_in_place_where_output_may_be_taken_back() -> None:
    order: list[str] = []

    @concurrent
    async def mark(name: str) -> str:
        order.append(name)
        await asyncio.sleep(0)
        return name

    page = PageTemplate(
        '${mark("a")}<p tal:on-error="string:E">${mark("b")}</p><p i18n:translate="">x ${mark("c")}</p>'
        '<b tal:content="mark(\'d\')"/><i tal:condition="mark(\'e\')">${mark("f")}</i>'
    )
    out = run(page.render_async(mark=mark))
    assert out == 'a<p>b</p><p>x c</p><b>d</b><i>f</i>'


def test_concurrent_value_error_and_cancellation_of_the_others() -> None:
    cancelled = []

    @concurrent
    async def slow() -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return 'never'

    @concurrent
    async def bad() -> str:
        await asyncio.sleep(0.01)
        raise KeyError('bad')

    page = PageTemplate('<div>\n${bad()}${slow()}</div>')
    with pytest.raises(KeyError) as error:
        run(page.render_async(bad=bad, slow=slow))
    assert 'Expression: "bad()"' in str(error.value)
    assert '(line 2: col 2)' in str(error.value)
    run(asyncio.sleep(0.01))
    assert cancelled == [True]


def test_cancelling_a_render_cancels_its_concurrent_tasks() -> None:
    cancelled = []

    @concurrent
    async def slow() -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return 'never'

    async def main() -> None:
        task = asyncio.ensure_future(PageTemplate('${slow()}${slow()}').render_async(slow=slow))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    run(main())
    assert cancelled == [True, True]


def test_concurrent_values_in_a_sync_render_are_values() -> None:
    call = cslow(1)
    try:
        assert PageTemplate('${v}').render(v=call).startswith('&lt;ConcurrentCall')
    finally:
        call.awaitable.close()  # ty: ignore[unresolved-attribute] - a coroutine


def test_offload_renders_in_a_worker_thread(tmp_path: Path) -> None:
    import threading

    seen: list[str] = []

    def where() -> str:
        seen.append(threading.current_thread().name)
        return 'x'

    page = PageTemplate('<p>${where()} ${n}</p>')
    assert run(page.render_async(offload=True, where=where, n=1)) == '<p>x 1</p>'
    assert seen and seen[0] != threading.main_thread().name
    (tmp_path / 't.txt').write_text('a${n}')
    text = chameleon_rs.PageTextTemplateFile(str(tmp_path / 't.txt'))
    assert run(text.render_async(offload=True, n='é')) == 'aé'.encode()


def test_partial_fan_out(tmp_path: Path, configured: Path) -> None:
    (tmp_path / 'card.pt').write_text('<li tal:define="d data">${title}:${d}${render_partial("inner.pt", v=d)}</li>')
    (tmp_path / 'inner.pt').write_text('<i>${v}</i>')

    async def slow(v: Any) -> Any:
        await asyncio.sleep(0.05)
        return v

    body = (
        '<ul><tal:x repeat="i range(6)">${render_partial("card.pt", title=i, data=slow(i * 10))}</tal:x></ul>'
        "<p title=\"${render_partial('inner.pt', v=slow('a&'))}\">"
        '${render_partial("inner.pt", v=1)}</p>'
    )
    plain = PageTemplate(body)
    fanned = PageTemplate(body, concurrent=True)
    start = time.perf_counter()
    want = run(plain.render_async(slow=slow))
    sequential = time.perf_counter() - start
    start = time.perf_counter()
    got = run(fanned.render_async(slow=slow))
    fan_out = time.perf_counter() - start
    assert got == want
    assert fan_out < sequential / 2, (fan_out, sequential)


def test_partial_fan_out_error_is_the_partials_render_error(tmp_path: Path, configured: Path) -> None:
    (tmp_path / 'bad.pt').write_text('<p tal:define="d data">${d["missing"]}</p>')
    page = PageTemplate('${render_partial("bad.pt", data=aw({}))}', concurrent=True)
    with pytest.raises(KeyError) as error:
        run(page.render_async(aw=aw))
    assert 'bad.pt' in str(error.value)


# -- Phase 11 review fixes -----------------------------------------------------------------


def _gc_over_visits(obj: Any) -> list[str]:
    """Referents of `obj` that the collector would see visited more often (by every tracked
    object) than they have references: `tp_traverse` must visit each reference once."""
    import collections

    gc.collect()
    refs = gc.get_referents(obj)
    mine = collections.Counter(id(r) for r in refs)
    everything = gc.get_objects()
    skip = {id(refs), id(everything)}
    visits: collections.Counter[int] = collections.Counter()
    for container in everything:
        if id(container) in skip:
            continue
        for r in gc.get_referents(container):
            if id(r) in mine:
                visits[id(r)] += 1
    bad = []
    seen: set[int] = set()
    for r in refs:
        if id(r) in seen:
            continue
        seen.add(id(r))
        # getrefcount's own argument, and this function's `refs` list holds `mine` of them.
        count = sys.getrefcount(r) - 1 - mine[id(r)]
        if visits[id(r)] > count:
            bad.append(f'{type(r).__name__}: {visits[id(r)]} visits, {count} references')
    del everything
    return bad


class _Pending:
    def __await__(self) -> Any:
        yield 'pending'
        return 'done'


def test_gc_visits_each_reference_once_while_suspended(tmp_path: Path, configured: Path) -> None:
    # A render suspended in a nested native partial (the same partial twice: its prepared
    # target is shared by units, call sites and the render's list), and in a macro of another
    # template.
    (tmp_path / 'p.pt').write_text('<em>${aw(1)}${render_partial("q.pt", aw=aw)}</em>')
    (tmp_path / 'q.pt').write_text('<b>${aw(2)}</b>')
    (tmp_path / 'layout.pt').write_text('<div metal:define-macro="m">${aw(3)}<i metal:define-slot="s"/></div>')
    page = PageTemplate(
        '<div>${render_partial("p.pt", aw=aw)}${render_partial("p.pt", aw=aw)}</div>'
        '<p metal:use-macro="layout.macros[\'m\']"><b metal:fill-slot="s">${aw(4)}</b></p>'
    )
    layout = PageTemplateFile(str(tmp_path / 'layout.pt'))
    a = chameleon_rs._native.render_page_async(page, None, {'aw': lambda v: _Pending(), 'layout': layout})
    assert isinstance(a, chameleon_rs._native.RenderAwaitable)
    steps = 0
    while True:
        try:
            a.send(None)
        except StopIteration:
            break
        steps += 1
        assert _gc_over_visits(a) == [], steps
    assert steps >= 6


def test_gc_visits_each_reference_once_while_filling_and_close() -> None:
    # The Fill phase: the render has finished and awaits the tasks of its placeholders.
    @concurrent
    async def slow(v: Any) -> Any:
        await asyncio.sleep(0.05)
        return v

    async def main() -> None:
        a = chameleon_rs._native.render_page_async(PageTemplate('${slow(1)}${slow(2)}'), None, {'slow': slow})
        assert isinstance(a, chameleon_rs._native.RenderAwaitable)
        a.send(None)  # the VM finishes; the first task is awaited
        await asyncio.sleep(0)  # the tasks start
        assert _gc_over_visits(a) == []
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert len(tasks) == 2
        a.close()
        await asyncio.sleep(0)
        assert all(t.cancelled() for t in tasks)
        ref = weakref.ref(slow)
        del a
        gc.collect()
        assert ref() is not None  # still ours; the render itself is gone without a trace

    run(main())


def test_a_render_in_the_fill_phase_is_collected() -> None:
    @concurrent
    async def slow() -> Any:
        await asyncio.sleep(10)

    class Holder:
        render: Any

    async def main() -> weakref.ref[Any]:
        holder = Holder()
        a = chameleon_rs._native.render_page_async(PageTemplate('${h}${slow()}'), None, {'slow': slow, 'h': holder})
        assert isinstance(a, chameleon_rs._native.RenderAwaitable)
        holder.render = a  # a cycle through the render's scope
        a.send(None)
        await asyncio.sleep(0)  # the task starts
        ref = weakref.ref(holder)
        del a, holder
        gc.collect()
        await asyncio.sleep(0)
        return ref

    assert run(main())() is None


# The placeholder the 1st render of a process got before the nonce was random (the review's
# probe): user text equal to it must stay text.
_OLD_FIRST_PLACEHOLDER = '\U0010fffd' + f'{0x9E3779B97F4A7C15 ^ (0x9E3779B97F4A7C15 >> 31):016x}' + '00000000\U0010fffd'


def test_text_equal_to_a_placeholder_stays_text() -> None:
    page = PageTemplate('<p title="${q}">x</p><div>${slow(v)}</div>${q}')
    out = run(page.render_async(slow=cslow, q=_OLD_FIRST_PLACEHOLDER, v='<b>"t"</b>'))
    assert out == page.render(slow=lambda v: v, q=_OLD_FIRST_PLACEHOLDER, v='<b>"t"</b>')
    assert '<b>' not in out


def test_render_overrides_in_subclasses_are_honoured(tmp_path: Path) -> None:
    class Page(PageTemplate):
        def render(self, encoding: Any = None, **kw: Any) -> str:
            kw.setdefault('who', 'default')
            return super().render(encoding, **kw).upper()

    page = Page('<p>${who}</p>')
    assert run(page.render_async()) == '<P>DEFAULT</P>'
    assert run(page.render_async(offload=True, who='x')) == '<P>X</P>'

    class Text(chameleon_rs.PageTextTemplateFile):
        def render(self, encoding: Any = None, **kw: Any) -> bytes:
            return b'[' + super().render(encoding, **kw) + b']'

    (tmp_path / 't.txt').write_text('a${n}')
    assert run(Text(str(tmp_path / 't.txt')).render_async(n=1)) == b'[a1]'


def test_a_failed_render_waits_for_its_cancelled_tasks() -> None:
    finished: list[str] = []

    @concurrent
    async def slow() -> str:
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0.01)  # cleanup that itself awaits
            finished.append('slow')
        return 'never'

    @concurrent
    async def bad() -> str:
        await asyncio.sleep(0.01)
        raise KeyError('bad')

    async def main() -> None:
        with pytest.raises(KeyError):
            await PageTemplate('${slow()}${bad()}').render_async(slow=slow, bad=bad)
        # Already done when the error arrives (TaskGroup semantics).
        assert finished == ['slow']

    run(main())


def test_a_cancelled_render_waits_for_its_tasks() -> None:
    finished: list[str] = []

    @concurrent
    async def slow() -> str:
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0.01)
            finished.append('slow')
        return 'never'

    async def main() -> None:
        task = asyncio.ensure_future(PageTemplate('${slow()}${slow()}').render_async(slow=slow))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == ['slow', 'slow']

    run(main())


def test_protocol_send_non_none_to_a_fresh_render_leaves_it_usable() -> None:
    a = _awaitable('a${x}', x=1)
    with pytest.raises(TypeError, match='just-started'):
        a.send(1)
    with pytest.raises(StopIteration) as stop:
        a.send(None)
    assert stop.value.value == 'a1'


def test_protocol_throw_instance_with_a_value_is_a_type_error() -> None:
    a = _awaitable('x')
    with pytest.raises(TypeError, match='instance exception may not have a separate value'):
        a.throw(KeyError('k'), 1)


def test_a_metaclass_await_does_not_make_instances_awaitable() -> None:
    class Meta(type):
        def __await__(cls) -> Any:
            yield from ()

    class Value(metaclass=Meta):
        def __str__(self) -> str:
            return 'value'

    assert run(PageTemplate('${v}').render_async(v=Value())) == 'value'


async def _later(value: Any) -> Any:
    await asyncio.sleep(0)
    return value


@concurrent
async def _later_concurrent(value: Any) -> Any:
    await asyncio.sleep(0)
    return value


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('<p>${structure: later(x)}</p>', '<p><b>x</b></p>'),
        ('<p>${structure: soon(x)}</p>', '<p><b>x</b></p>'),
        ('<p title="${structure: later(x)}"/>', '<p title="<b>x</b>"/>'),
        ('<p tal:replace="structure: later(x)"/>', '<b>x</b>'),
        ('<p tal:content="structure later(x)"/>', '<p><b>x</b></p>'),
    ],
)
def test_structure_of_an_awaitable_is_awaited(source: str, expected: str) -> None:
    # The awaitable once became `Markup(repr)`: `<coroutine object ...>` written raw, never run.
    async def main() -> str:
        return await PageTemplate(source).render_async(x='<b>x</b>', later=_later, soon=_later_concurrent)

    with warnings.catch_warnings():
        # A coroutine that never ran warns when collected.
        warnings.simplefilter('error', RuntimeWarning)
        assert asyncio.run(main()) == expected
        gc.collect()


@pytest.mark.parametrize('prefix', ['', 'structure: ', 'markdown: '])
def test_a_concurrent_call_under_structure_or_markdown_stays_concurrent(prefix: str) -> None:
    # Wrapping a `@concurrent` call in `structure:` / `markdown:` once made it run on its own.
    chameleon_rs.configure(markdown=True)
    running = 0
    most = 0

    @concurrent
    async def slow(value: str) -> str:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return value

    source = ''.join(f'<p>${{{prefix}slow("x")}}</p>' for _ in range(3))
    try:
        asyncio.run(PageTemplate(source).render_async(slow=slow))
    finally:
        chameleon_rs._settings.clear()
    assert most == 3
