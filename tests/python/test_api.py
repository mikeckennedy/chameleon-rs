"""Phase 7, the Python API: Chameleon's configuration surface, compile-error messages, the
loader, thread-safe cooking and `auto_reload`, `CHAMELEON_*` settings, `ExpressionEvaluator`,
the native-backed `tokenize` / `parser` modules, and macros, each compared with the reference
Chameleon where it has an observable result."""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import chameleon
import chameleon.astutil
import chameleon.compiler
import chameleon.loader
import chameleon.parser
import chameleon.tales
import chameleon.tokenize
import chameleon.utils
import chameleon_rs
import chameleon_rs.astutil
import chameleon_rs.compiler
import chameleon_rs.loader
import chameleon_rs.parser
import chameleon_rs.tales
import chameleon_rs.template
import chameleon_rs.tokenize
import chameleon_rs.utils
import pytest
from chameleon_rs import exc

INPUTS = Path(__file__).parents[1] / 'upstream' / 'chameleon_tests' / 'inputs'
ADDRESS = re.compile(r'0x[0-9a-fA-F]+')


def outcome(func: Any) -> tuple[str, ...]:
    try:
        value = func()
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        # (A nested error's message names its class with the module: ours or Chameleon's.)
        return ('raise', type(e).__name__, ADDRESS.sub('0x?', str(e)).replace('chameleon_rs.', 'chameleon.'))
    return ('ok', value if isinstance(value, str | bytes | tuple) else type(value).__name__)


# --- compile errors: the whole message, `- Source:` lines included -------------------------

COMPILE_ERRORS = [
    '<p tal:define="a b c d">x</p>',
    '<div>\n  <p tal:content="1 +">x</p></div>',
    '<p tal:foo="x"/>',
    '<a></b>',
    '<div>\n<a>\n</b></div>',
    '<p i18n:name="x"/>',
    "<tal:block replace='bad /// ' />",
    '<p tal:repeat="a">x</p>',
    '<!-- a -- b -->',
    '<p xmlns:x="u" y:z="1"/>',
    '<div tal:define="__dummy 1"/>',
    '<p tal:attributes="dummy">x</p>',
    '<p>\n\n     ${a b}</p>',
    '<p>${ä ö}</p>',
    '<p tal:content="string:${a b}">x</p>',
    '<p metal:fill-slot="x" metal:define-slot="y">x</p>',
]


@pytest.mark.parametrize('body', COMPILE_ERRORS)
def test_compile_error_messages(body: str) -> None:
    def compile_with(engine: Any) -> Any:
        return lambda: engine.PageTemplate(body)

    assert outcome(compile_with(chameleon_rs)) == outcome(compile_with(chameleon))


@pytest.mark.parametrize('body', COMPILE_ERRORS)
def test_compile_error_messages_of_files(body: str, tmp_path: Path) -> None:
    path = tmp_path / 'broken.pt'
    path.write_text(body)

    def compile_with(engine: Any) -> Any:
        return lambda: engine.PageTemplateFile(str(path)).cook_check()

    assert outcome(compile_with(chameleon_rs)) == outcome(compile_with(chameleon))


@pytest.mark.parametrize('body', ['<p tal:content="1 +">x</p>', '<p>\n ${a b}</p>'])
def test_non_strict_error_messages(body: str) -> None:
    def render(engine: Any) -> Any:
        return lambda: engine.PageTemplate(body, strict=False)()

    assert outcome(render(chameleon_rs)) == outcome(render(chameleon))


def test_template_error_api() -> None:
    token = chameleon_rs.tokenize.Token('stuff', 5, 'more\nstuff', 'mystuff.txt')
    error = exc.TemplateError('message', token)
    assert (error.offset, error.location, error.filename, error.token) == (5, (2, 0), 'mystuff.txt', 'stuff')
    assert repr(error) == "TemplateError('message', 'stuff')"
    plain = exc.LanguageError('message', 'tok')
    assert (plain.offset, plain.location, plain.filename) == (0, (0, 0), '')
    import copy
    import pickle

    assert repr(copy.copy(error)) == repr(error)
    assert repr(pickle.loads(pickle.dumps(error, -1))) == repr(error)


def test_lone_surrogates_in_the_source_render() -> None:
    # Chameleon renders them; chameleon-rs maps them through a private-use block (Phase 8;
    # every position is compared with Chameleon in test_surrogates.py).
    body = '<p>ab\ud800c ${x}</p>'
    assert chameleon_rs.PageTemplate(body)(x='a\udc80') == chameleon.PageTemplate(body)(x='a\udc80')


# --- configuration -----------------------------------------------------------------------


def both(source: str, kwargs: dict[str, Any] | None = None, **config: Any) -> None:
    kwargs = kwargs or {}

    def render(engine: Any) -> Any:
        return lambda: engine.PageTemplate(source, **config)(**kwargs)

    assert outcome(render(chameleon_rs)) == outcome(render(chameleon))


# Chameleon's generated code imports a marker object by module and name.
CUSTOM_MARKER = chameleon.utils.ImportableMarker(__name__, 'CUSTOM')


@pytest.mark.parametrize(
    'source',
    [
        '<p tal:content="m">default</p>',
        '<p title="t" tal:attributes="title m">x</p>',
        '<p tal:content="default is m">x</p>',
        '<p tal:replace="default">x</p>',
        '<input checked="checked" tal:attributes="checked m" />',
        '<p>${m}</p>',
    ],
)
def test_custom_default_marker(source: str) -> None:
    def render(engine: Any) -> Any:
        marker = engine.astutil.Symbol(CUSTOM_MARKER)
        return lambda: engine.PageTemplate(source, default_marker=marker)(m=CUSTOM_MARKER)

    want = outcome(render(chameleon))
    assert want[0] == 'ok'
    assert outcome(render(chameleon_rs)) == want


def test_default_marker_attribute() -> None:
    template = chameleon_rs.PageTemplate('<p/>')
    assert template.default_marker.value is chameleon_rs.tales.DEFAULT_MARKER
    assert template(x=1) == '<p/>'


def doubled_tokenizer(module: Any) -> Any:
    """A tokenizer whose tokens are not the body's: it tokenizes a changed text."""

    def tokenize(body: str, filename: str | None = None) -> Any:
        return module.iter_xml(body.replace('[[', '${').replace(']]', '}'), filename)

    return tokenize


@pytest.mark.parametrize(
    'source', ['<p tal:content="x">[[x]]</p>', '<div>\n<b>[[x * 2]]</b>\n</div>', 'plain [[x]] text']
)
def test_custom_tokenizer(source: str) -> None:
    got = chameleon_rs.PageTemplate(source, tokenizer=doubled_tokenizer(chameleon_rs.tokenize))(x=3)
    want = chameleon.PageTemplate(source, tokenizer=doubled_tokenizer(chameleon.tokenize))(x=3)
    assert got == want


def test_custom_tokenizer_errors_point_into_its_tokens() -> None:
    source = '<p>\n[[1 +]]</p>'
    with pytest.raises(exc.ExpressionError) as e:
        chameleon_rs.PageTemplate(source, tokenizer=doubled_tokenizer(chameleon_rs.tokenize))
    assert e.value.location == (2, 2)


def test_html5_boolean_attributes() -> None:
    """The opt-in HTML5 set: Chameleon's 12 plus z3c.pt's 20 (#11)."""
    html5 = chameleon_rs.template.HTML5_BOOLEAN_ATTRIBUTES
    assert len(html5) == 32
    assert html5 >= set(chameleon_rs.template.BOOLEAN_HTML_ATTRIBUTES)
    source = '<input tal:attributes="required x; formnovalidate x; checked x; hidden x"/>'
    template = chameleon_rs.PageTemplate(source, boolean_attributes=html5)
    assert (
        template(x=True)
        == '<input required="required" formnovalidate="formnovalidate" checked="checked" hidden="True"/>'
    )
    assert template(x=False) == '<input hidden="False"/>'
    # The default stays Chameleon's.
    assert chameleon_rs.PageTemplate(source)(x=True) == chameleon.PageTemplate(source)(x=True)


def test_configuration_options() -> None:
    both('<p>${x}</p>', {'x': object()}, translate=None)
    both('<p>${x}</p>', {'x': 'Тест'.encode('windows-1251')}, encoding='windows-1251')
    both('<input checked="${x}" selected="${x}"/>', {'x': True}, boolean_attributes={'selected'})
    both('<p title="a" alt="b">x ${y}</p>', {'y': 1}, implicit_i18n_translate=True, implicit_i18n_attributes={'title'})
    both('<p data-tal-content="x">y</p>', {'x': 1}, enable_data_attributes=True)
    both('<!-- ${x} -->', {'x': 1}, enable_comment_interpolation=False)
    both('<p v-on:click="x">y</p>', restricted_namespace=False)
    both('<p\n   class="a"\n   tal:attributes="id 1">x</p>', trim_attribute_space=True)
    both('<p>${x}</p>', strict=False)
    both('<p>${extra}</p>', extra_builtins={'extra': 'E'})
    both('<p>${y}</p>', {'y': 1}, default_expression='python')


def test_translate_and_on_error_handler() -> None:
    def translate(msgid: str, **kwargs: Any) -> str:
        return f'<{msgid}:{kwargs.get("target_language")}>'

    both('<p i18n:translate="">Hello</p>', {'target_language': 'de'}, translate=translate)
    seen: list[list[BaseException]] = [[], []]
    for engine, errors in zip((chameleon, chameleon_rs), seen, strict=True):
        engine.PageTemplate('<p tal:on-error="string:E">${x}</p>', on_error_handler=errors.append)()
    assert [type(e) for e in seen[0]] == [type(e) for e in seen[1]] == [NameError]


def test_value_repr_in_render_errors() -> None:
    both('<p tal:define="y x">${y.z}</p>', {'x': 'value'}, value_repr=lambda v: f'R({v!r})')


def test_debug_keep_source_and_keep_body() -> None:
    template = chameleon_rs.PageTemplate('<p>${x}</p>', debug=True)
    assert template.debug
    assert isinstance(template.loader, chameleon_rs.loader.ModuleLoader)
    plain = chameleon_rs.PageTemplate('<p>${x}</p>')
    assert not plain.debug
    assert 'debug' not in plain.__dict__
    assert isinstance(plain.loader, chameleon_rs.loader.MemoryLoader)
    assert not hasattr(plain, 'source')
    assert not hasattr(plain, 'body')
    kept = chameleon_rs.PageTemplate('<p>${x}</p>', keep_source=True, keep_body=True)
    assert kept.body == '<p>${x}</p>'
    assert 'x' in kept.source  # a dump of the program (D11)


def test_digest() -> None:
    template = chameleon_rs.PageTemplate('<html></html>')
    assert re.fullmatch('[0-9a-f]{32}', template.digest('<html></html>', ['a']))
    template.filename = None
    assert template.digest('<html></html>', [])


def test_render_function_set_on_the_instance() -> None:
    template = chameleon_rs.PageTemplate('<p>x</p>')
    calls = []

    def _render(stream: list[str], econtext: Any, rcontext: Any, **kw: Any) -> None:
        calls.append(kw)
        stream.append(f'rendered {econtext["a"]}')

    template._render = _render  # ty: ignore[invalid-assignment] - Chameleon's render protocol
    assert template(a=1) == 'rendered 1'
    assert calls == [{'target_language': None}]


def test_recook_keeps_removed_macros() -> None:
    """Chameleon never removes a macro's `_render_<name>` from the template when it compiles
    new source: a macro the new source lacks still renders its last compilation, `names` lists
    every name in the order it was first defined, and a macro defined again is the new one."""
    use = '<p metal:use-macro="t.macros[n]"/>'
    for engine in (chameleon, chameleon_rs):
        template = engine.PageTemplate(
            '<div metal:define-macro="b-x">B</div><div metal:define-macro="a">A</div><div metal:define-macro="c">C</div>'
        )
        assert template.macros.names == ['b_x', 'a', 'c']
        template.cook('<div metal:define-macro="c">C2</div><div metal:define-macro="d">D</div>')
        assert template.macros.names == ['b_x', 'a', 'c', 'd'], engine
        rendered = {n: engine.PageTemplate(use)(t=template, n=n) for n in ('b-x', 'a', 'c', 'd')}
        assert rendered == {'b-x': '<div>B</div>', 'a': '<div>A</div>', 'c': '<div>C2</div>', 'd': '<div>D</div>'}
        template.cook('<div metal:define-macro="b-x">B3</div>')
        assert template.macros.names == ['b_x', 'a', 'c', 'd']
        assert engine.PageTemplate(use)(t=template, n='b-x') == '<div>B3</div>'
        assert engine.PageTemplate(use)(t=template, n='d') == '<div>D</div>'
        with pytest.raises(KeyError, match="Macro does not exist: 'e'"):
            template.macros['e']


def test_recook_keeps_macro_of_macro_renderer(tmp_path: Path) -> None:
    """pyramid_chameleon's `test_macro_template_reload`: a template whose `cook` renders one
    macro keeps rendering it after `cook` is given source without that macro."""
    path = tmp_path / 'page.pt'
    path.write_text('<html>\nOutside macro\n<div metal:define-macro="foo">\n  Hello!\n</div>\n</html>\n')
    for engine in (chameleon, chameleon_rs):

        class PyramidPageTemplateFile(engine.PageTemplateFile):  # ty: ignore[unsupported-base]
            def cook(self, body: str) -> None:
                super().cook(body)
                if self.macro:
                    self._render = self.macros[self.macro].include

        template = PyramidPageTemplateFile(str(path), macro='foo')
        first = template()
        template.cook('<html>\nOutside macro\n\n  Hello!\n\n</html>\n\n')
        assert template() == first, engine


def test_render_function_set_by_cook(tmp_path: Path) -> None:
    """pyramid_chameleon's `macro=` renderer: `cook` sets `_render` to a macro's `include`,
    so even the first render of a lazily compiled file template renders only the macro."""
    path = tmp_path / 'page.pt'
    path.write_text('<html><div metal:define-macro="body">Body ${x}</div><p>rest</p></html>')
    for engine in (chameleon, chameleon_rs):

        class PyramidPageTemplateFile(engine.PageTemplateFile):  # ty: ignore[unsupported-base]
            def cook(self, body: str) -> None:
                super().cook(body)
                if self.macro:
                    self._render = self.macros[self.macro].include

        template = PyramidPageTemplateFile(str(path), macro='body')
        assert template(x=1) == '<div>Body 1</div>'
        assert template(x=2) == '<div>Body 2</div>'
        whole = PyramidPageTemplateFile(str(path), macro=None)
        assert whole(x=3) == '<html><div>Body 3</div><p>rest</p></html>'


def test_subclass_class_attributes() -> None:
    class Template(chameleon_rs.PageTemplate):
        strict = False
        extra_builtins = {'site': 'S'}  # noqa: RUF012 - Chameleon's class attribute

        @staticmethod
        def translate(msgid: str, **kwargs: Any) -> str:
            return msgid.upper()

    assert Template('<p i18n:translate="">hi ${site}</p>')() == '<p>HI S</p>'
    assert Template('<p>${1 +}</p>') is not None


# --- loader --------------------------------------------------------------------------------


def make_tree(root: Path) -> Path:
    (root / 'a').mkdir()
    (root / 'b').mkdir()
    (root / 'a' / 'page.pt').write_text('<p>a ${x}</p>')
    (root / 'b' / 'page.pt').write_text('<p>b ${x}</p>')
    (root / 'b' / 'other.pt').write_text('<p>other</p>')
    (root / 'b' / 'note.txt').write_text('note ${x}')
    (root / 'b' / 'layout.pt').write_text('<html metal:define-macro="m"><b metal:define-slot="s"/></html>')
    (root / 'b' / 'uses.pt').write_text(
        '<html metal:use-macro="load: layout.pt"><i metal:fill-slot="s">${x}</i></html>'
    )
    return root


def test_page_template_loader(tmp_path: Path) -> None:
    make_tree(tmp_path)
    paths = [str(tmp_path / 'a'), str(tmp_path / 'b')]
    for engine in (chameleon, chameleon_rs):
        loader = engine.PageTemplateLoader(paths, '.pt')
        page = loader['page']
        assert page.filename == str(tmp_path / 'a' / 'page.pt')
        assert page(x=1) == '<p>a 1</p>'
        assert loader.load('page.pt') is loader.load('page.pt')
        assert loader['other'](x=1) == '<p>other</p>'
        assert loader['uses'](x=2) == '<html><i>2</i></html>'
        assert loader.load('note.txt', 'text')(x=3) == b'note 3'
        assert isinstance(loader.load('note.txt', 'text'), engine.PageTextTemplateFile)
        with pytest.raises(ValueError, match='Template not found'):
            loader['missing']
        absolute = loader.load(str(tmp_path / 'b' / 'page.pt'))
        assert absolute(x=4) == '<p>b 4</p>'


def test_loader_formats_and_keywords(tmp_path: Path) -> None:
    make_tree(tmp_path)

    class Upper(chameleon_rs.PageTemplateFile):
        @staticmethod
        def translate(msgid: str, **kwargs: Any) -> str:
            return msgid.upper()

    loader = chameleon_rs.PageTemplateLoader(str(tmp_path / 'b'), formats={'xml': Upper}, strict=False)
    template = loader['page.pt']
    assert isinstance(template, Upper)
    assert template.strict is False


def test_package_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / 'rs_templates_pkg'
    (package / 'templates').mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'templates' / 'main.pt').write_text('<p metal:use-macro="load: macro.pt"/>')
    (package / 'templates' / 'macro.pt').write_text('<b>${x}</b>')
    monkeypatch.syspath_prepend(str(tmp_path))
    for engine in (chameleon, chameleon_rs):
        loader = engine.PageTemplateLoader(auto_reload=True)
        template = loader['rs_templates_pkg:templates/main.pt']
        assert template(x=1) == '<b>1</b>'
        assert template._v_last_read is not None
        assert template.package_name == 'rs_templates_pkg'


def test_search_path_options(tmp_path: Path) -> None:
    make_tree(tmp_path)
    (tmp_path / 'b' / 'include.pt').write_text('<p metal:use-macro="load: page.pt"/>')
    for engine in (chameleon, chameleon_rs):
        # The template's directory comes first, then the search path.
        t = engine.PageTemplateFile(str(tmp_path / 'b' / 'include.pt'), search_path=[str(tmp_path / 'a')])
        assert t(x=1) == '<p>b 1</p>'

        class NoRelative(engine.PageTemplateFile):  # ty: ignore[unsupported-base]
            prepend_relative_search_path = False

        t = NoRelative(str(tmp_path / 'b' / 'include.pt'), search_path=str(tmp_path / 'a'))
        assert t(x=1) == '<p>a 1</p>'


def test_pathlib_search_paths_and_specs(tmp_path: Path) -> None:
    # malthe/chameleon#342: Chameleon raises "'PosixPath' object is not iterable" for a single
    # `Path` search path and has no `strip` for a `Path` spec; chameleon_rs takes any
    # `os.PathLike` wherever it takes a path string.
    make_tree(tmp_path)
    (tmp_path / 'b' / 'partial.pt').write_text("<div>${render_partial('other.pt')}</div>")
    loader = chameleon_rs.PageTemplateLoader(tmp_path / 'b', '.pt')
    assert loader['page'](x=1) == '<p>b 1</p>'
    assert loader[Path('page.pt')](x=2) == '<p>b 2</p>'
    assert loader['uses'](x=3) == '<html><i>3</i></html>'
    assert loader['partial']() == '<div><p>other</p></div>'
    assert loader.search_path == [str(tmp_path / 'b')]
    loader = chameleon_rs.PageTemplateLoader((tmp_path / 'a', str(tmp_path / 'b')))
    assert loader['other.pt']() == '<p>other</p>'
    t = chameleon_rs.PageTemplateFile(tmp_path / 'b' / 'uses.pt', search_path=tmp_path / 'a')
    assert t(x=4) == '<html><i>4</i></html>'
    with pytest.raises(TypeError, match='not iterable'):
        chameleon.PageTemplateLoader(tmp_path / 'b')['page.pt']  # ty: ignore[invalid-argument-type]


def test_search_path_list_is_kept() -> None:
    # As in Chameleon: a list of strings is the loader's own `search_path`, not a copy.
    paths = ['a']
    for engine in (chameleon, chameleon_rs):
        assert engine.PageTemplateLoader(paths).search_path is paths


def test_auto_reload(tmp_path: Path) -> None:
    path = tmp_path / 't.pt'
    path.write_text('<p>one</p>')
    os.utime(path, (1, 1))
    template = chameleon_rs.PageTemplateFile(str(path), auto_reload=True)
    assert template() == '<p>one</p>'
    path.write_text('<p>two</p>')
    os.utime(path, (2, 2))
    assert template() == '<p>two</p>'
    assert not template.cook_check()
    still = chameleon_rs.PageTemplateFile(str(path))
    assert still() == '<p>two</p>'
    path.write_text('<p>three</p>')
    os.utime(path, (3, 3))
    assert still() == '<p>two</p>'


@pytest.fixture
def lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    """File templates compile on first use even when `CHAMELEON_EAGER` is set."""
    monkeypatch.setattr(chameleon_rs.template, 'EAGER_PARSING', False)


def write_atomically(path: Path, text: str, mtime: int) -> None:
    temp = path.with_suffix('.tmp')
    temp.write_text(text)
    os.utime(temp, (mtime, mtime))
    os.replace(temp, path)


@pytest.mark.usefixtures('lazy')
def test_cook_check_and_auto_reload_are_thread_safe(tmp_path: Path) -> None:
    path = tmp_path / 't.pt'
    path.write_text('<p>0 ${x}</p>')
    template = chameleon_rs.PageTemplateFile(str(path), auto_reload=True)
    cooks = []
    original = template.cook

    def counting_cook(body: str) -> None:
        cooks.append(body)
        time.sleep(0.001)  # widen the window in which another thread could see a half-cooked template
        original(body)

    template.cook = counting_cook  # ty: ignore[invalid-assignment]
    stop = threading.Event()
    errors: list[BaseException] = []
    outputs: set[str] = set()

    def render() -> None:
        while not stop.is_set():
            try:
                outputs.add(template(x='x'))
            except BaseException as e:  # noqa: BLE001 - collected for the assertion
                errors.append(e)

    threads = [threading.Thread(target=render) for _ in range(8)]
    for t in threads:
        t.start()
    for version in range(1, 30):
        write_atomically(path, f'<p>{version} ${{x}}</p>', version * 10)
        time.sleep(0.002)
    stop.set()
    for t in threads:
        t.join()
    assert errors == []
    assert outputs <= {f'<p>{v} x</p>' for v in range(30)}
    # Versions are compiled in order (a thread never reloads an older one) and seldom twice
    # (only when the file changes between the check and the read), though eight threads
    # saw each change.
    versions = [int(re.findall(r'\d+', body)[0]) for body in cooks]
    assert versions == sorted(versions)
    assert len(cooks) < 40
    assert template(x='x') == '<p>29 x</p>'


@pytest.mark.usefixtures('lazy')
def test_many_threads_compile_a_file_template_once(tmp_path: Path) -> None:
    path = tmp_path / 't.pt'
    path.write_text('<p>${x}</p>')
    template = chameleon_rs.PageTemplateFile(str(path))
    cooks = []
    original = template.cook

    def counting_cook(body: str) -> None:
        cooks.append(body)
        time.sleep(0.01)
        original(body)

    template.cook = counting_cook  # ty: ignore[invalid-assignment]
    barrier = threading.Barrier(16)
    results: list[str] = []

    def render(n: int) -> None:
        barrier.wait()
        results.append(template(x=n))

    threads = [threading.Thread(target=render, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(cooks) == 1
    assert sorted(results) == sorted(f'<p>{n}</p>' for n in range(16))


# --- CHAMELEON_* environment settings -------------------------------------------------------


def run_python(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    environ = {k: v for k, v in os.environ.items() if not k.upper().startswith('CHAMELEON_')}
    environ.update(env)
    return subprocess.run(
        [sys.executable, '-c', textwrap.dedent(code)], capture_output=True, text=True, env=environ, check=False
    )


def test_environment_settings(tmp_path: Path) -> None:
    broken = tmp_path / 'broken.pt'
    broken.write_text('<p tal:content="1 +">x</p>')
    code = f"""
        import chameleon_rs
        from chameleon_rs import config
        print(config.EAGER_PARSING, config.DEBUG_MODE, config.AUTO_RELOAD, config.CACHE_DIRECTORY)
        try:
            chameleon_rs.PageTemplateFile({str(broken)!r})
            print('lazy')
        except chameleon_rs.TemplateError:
            print('eager')
        t = chameleon_rs.PageTemplate('<p/>')
        print(t.debug, t.auto_reload if hasattr(t, 'auto_reload') else '-', type(t.loader).__name__,
              chameleon_rs.PageTemplateFile.auto_reload, hasattr(t, 'source'))
    """
    plain = run_python(code)
    assert plain.stdout.split('\n')[:3] == ['False False False None', 'lazy', 'False - MemoryLoader False False']
    eager = run_python(code, CHAMELEON_EAGER='true', CHAMELEON_RELOAD='1', CHAMELEON_DEBUG='yes')
    assert eager.stdout.split('\n')[:3] == ['True True True None', 'eager', 'True - ModuleLoader True True']
    cached = run_python(code, CHAMELEON_CACHE=str(tmp_path))
    assert cached.stdout.split('\n')[0] == f'False False False {tmp_path}'
    assert cached.stdout.split('\n')[2].split()[2] == 'ModuleLoader'
    missing = run_python(code, CHAMELEON_CACHE=str(tmp_path / 'missing'))
    assert 'ValueError: Cache directory does not exist' in missing.stderr
    unknown = run_python('import logging; logging.basicConfig(); import chameleon_rs', CHAMELEON_FOO='1')
    assert 'unknown environment variable set: "CHAMELEON_FOO"' in unknown.stderr


# --- ExpressionEvaluator -------------------------------------------------------------------


@pytest.mark.parametrize(
    ('kind', 'expression', 'scope'),
    [
        ('python', 'foo + boo', {'boo': 'baz'}),
        ('string', '${foo}-$boo', {'boo': 'baz'}),
        ('exists', 'nothing_here', {}),
        ('not', 'boo', {'boo': ''}),
        ('python', 'missing', {}),
        ('python', 'boo | foo', {}),
        ('python', '"a;b" + foo', {}),
    ],
)
def test_expression_evaluator(kind: str, expression: str, scope: dict[str, Any]) -> None:
    def evaluate(engine: Any) -> Any:
        tales = engine.tales
        types = {
            'python': tales.PythonExpr,
            'string': tales.StringExpr,
            'exists': tales.ExistsExpr,
            'not': tales.NotExpr,
        }
        parser = tales.ExpressionParser(types, 'python')
        factory = functools.partial(engine.compiler.ExpressionEngine, parser)
        evaluator = engine.compiler.ExpressionEvaluator(factory, {'foo': 'bar'})
        econtext = engine.utils.Scope(scope)
        return lambda: (evaluator(econtext, {}, kind, expression), econtext['_result'])

    got, want = outcome(evaluate(chameleon_rs)), outcome(evaluate(chameleon))
    if want[0] == 'raise':
        assert got[:2] == want[:2]
    else:
        assert got == want


def test_expression_evaluator_curries() -> None:
    parser = chameleon_rs.tales.ExpressionParser({'python': chameleon_rs.tales.PythonExpr}, 'python')
    evaluator = chameleon_rs.compiler.ExpressionEvaluator(
        functools.partial(chameleon_rs.compiler.ExpressionEngine, parser), {'foo': 'bar'}
    )
    python = evaluator(chameleon_rs.utils.Scope({'boo': 'baz'}), {}, 'python')
    assert python('foo + boo') == 'barbaz'
    assert python('foo + boo') == 'barbaz'


# --- tokenize and parser (native-backed compat modules) -------------------------------------


def normalize(value: Any) -> Any:
    """Tokens with their type and position, recursively."""
    if isinstance(value, str):
        pos = getattr(value, 'pos', None)
        return (type(value).__name__, str(value), pos, getattr(value, 'filename', None))
    if isinstance(value, OrderedDict | dict):
        return (type(value).__name__, sorted(((normalize(k), normalize(v)) for k, v in value.items()), key=repr))
    if isinstance(value, list | tuple):
        return [normalize(v) for v in value]
    return value


FILES = sorted(p for p in INPUTS.iterdir() if p.suffix in {'.pt', '.html', '.xml'})


@pytest.mark.parametrize('path', FILES, ids=lambda p: p.name)
def test_iter_xml_and_element_parser(path: Path) -> None:
    body = chameleon.utils.read_encoded(path.read_bytes())
    namespaces: dict[str | None, str] = {
        'xmlns': 'http://www.w3.org/2000/xmlns/',
        'xml': 'http://www.w3.org/XML/1998/namespace',
    }
    results = []
    for engine in (chameleon, chameleon_rs):
        tokens = list(engine.tokenize.iter_xml(body, 'f.pt'))
        try:
            items = list(engine.parser.ElementParser(iter(tokens), namespaces, False))
        except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
            items = ('raise', type(e).__name__, str(e))
        results.append((normalize(tokens), [t.source is body for t in tokens], normalize(items)))
    assert results[1] == results[0]


def test_element_parser_errors_and_foreign_tokens() -> None:
    errors = []
    for engine in (chameleon, chameleon_rs):
        with pytest.raises(engine.exc.ParseError) as e:
            list(engine.parser.ElementParser(engine.tokenize.iter_xml('<a>\n</b></a></a>', 'f.pt'), {}))
        errors.append((e.value.offset, e.value.location, e.value.token, e.value.filename, str(e.value)))
    assert errors[1] == errors[0]
    # Tokens from several sources, and plain strings: the items' strings are slices of them.
    first = chameleon.tokenize.Token('<a x="1">', 5, 'xxxxx<a x="1">', 'one.pt')
    second = chameleon.tokenize.Token('</a>', 2, '..</a>', 'two.pt')
    streams: tuple[list[Any], ...] = (['<a x="1">', 't', '</a>'], [first, 't', second])
    for stream in streams:
        results = []
        for engine in (chameleon, chameleon_rs):
            tokens = [engine.tokenize.Token(t, t.pos, t.source, t.filename) if hasattr(t, 'pos') else t for t in stream]
            results.append(normalize(list(engine.parser.ElementParser(tokens, {}))))
        assert results[1] == results[0]


def test_token() -> None:
    token = chameleon_rs.tokenize.Token('abc', 1)
    assert isinstance(token[1:], chameleon_rs.tokenize.Token)
    assert token[1:].pos == 2
    assert chameleon_rs.parser.match_double_hyphen.match('-- >')
    assert not chameleon_rs.parser.match_double_hyphen.match('-->')


# --- macros --------------------------------------------------------------------------------

MACROS = '<div metal:define-macro="a-b">A${x}</div><p metal:define-macro="c">C</p>'


def test_macros_api() -> None:
    for engine in (chameleon, chameleon_rs):
        template = engine.PageTemplate(MACROS)
        assert sorted(template.macros.names) == ['a_b', 'c']
        assert template['a-b'] is not None
        with pytest.raises(KeyError, match="Macro does not exist: 'zz'"):
            template.macros['zz']
        user = engine.PageTemplate('<x metal:use-macro="t[\'a-b\']"/>|<y metal:use-macro="t.macros[\'c\']"/>')
        assert user(t=template, x=1) == '<div>A1</div>|<p>C</p>'
        # A render function needs the render's `__translate`, `__decode` and
        # `__on_error_handler`.
        with pytest.raises(KeyError, match='__translate'):
            template.include([], engine.utils.Scope({'x': 2}), {})
        stream: list[str] = []
        scope = {'x': 2, '__translate': str, '__decode': bytes.decode, '__on_error_handler': None}
        template.include(stream, engine.utils.Scope(scope), {})
        assert ''.join(stream) == '<div>A2</div><p>C</p>'


# --- compat.install() without stock Chameleon -----------------------------------------------

COMPAT = """
    import importlib.abc
    import sys

    class Block(importlib.abc.MetaPathFinder):
        # Stock Chameleon is installed in this venv; the compat package must not need it.
        def find_spec(self, name, path=None, target=None):
            if name == 'chameleon' or name.startswith('chameleon.'):
                raise ImportError(f'stock Chameleon is blocked: {name}')
            return None

    block = Block()
    sys.meta_path.insert(0, block)

    import chameleon_rs.compat

    assert not chameleon_rs.compat.installed()
    chameleon_rs.compat.install()
    assert chameleon_rs.compat.installed()
    sys.meta_path.remove(block)

    import chameleon
    from chameleon import PageTemplate, PageTemplateFile, PageTemplateLoader, TemplateError
    from chameleon.astutil import Builtin, NameLookupRewriteVisitor, Symbol
    from chameleon.codegen import template
    from chameleon.compiler import ExpressionEngine, ExpressionEvaluator
    from chameleon.exc import ExpressionError, RenderError
    from chameleon.loader import TemplateLoader
    from chameleon.parser import ElementParser
    from chameleon.tales import DEFAULT_MARKER, ExpressionParser
    from chameleon.tokenize import Token, iter_xml
    from chameleon.utils import Scope
    from chameleon.zpt.loader import TemplateLoader as ZPTLoader
    from chameleon.zpt.template import PageTextTemplate
    import chameleon.zpt

    assert chameleon is chameleon_rs and PageTemplateLoader is ZPTLoader
    assert chameleon.zpt.template.PageTemplate is chameleon_rs.PageTemplate
    print(PageTemplate('<p tal:repeat="i items">${i}</p>')(items=[1, 2]))
    print(PageTextTemplate('${a} & b')(a='<'))
    try:
        PageTemplate('<p tal:content="1 +">x</p>')
    except ExpressionError as e:
        print(type(e).__module__, e.offset)
    for name in ('chameleon.nodes', 'chameleon.zpt.program', 'chameleon._native', 'chameleon._rewrite', 'chameleon.compat'):
        try:
            __import__(name)
        except ModuleNotFoundError as e:
            assert e.name == name, e
        else:
            raise AssertionError(name)
    print('no internals')
    import importlib
    import chameleon_rs._native
    for name in chameleon_rs.compat.MODULES:
        assert importlib.import_module('chameleon.' + name) is importlib.import_module('chameleon_rs.' + name), name
    assert chameleon.utils.Scope is chameleon_rs._native.Scope
    assert chameleon.tales.TalesExpr is chameleon_rs.tales.TalesExpr
    loaded = sorted({m.__name__ for n, m in sys.modules.items() if n.split('.')[0] == 'chameleon' and m})
    assert all(name.startswith('chameleon_rs') for name in loaded), loaded
    assert not any(n == 'chameleon' or n.startswith('chameleon.') for n, m in sys.modules.items()
                   if m is not None and not m.__name__.startswith('chameleon_rs')), 'stock Chameleon was imported'
    print('ok')
"""


def test_compat_install_without_stock_chameleon() -> None:
    result = run_python(COMPAT)
    assert result.stderr == ''
    assert result.stdout.split('\n') == [
        '<p>1</p>',
        '<p>2</p>',
        '< & b',
        'chameleon_rs.exc 16',
        'no internals',
        'ok',
        '',
    ]


def test_compat_install_replaces_stock_chameleon() -> None:
    result = run_python("""
        import warnings
        import chameleon, chameleon.zpt.template
        stock = chameleon.PageTemplate
        import chameleon_rs.compat
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            chameleon_rs.compat.install()
            chameleon_rs.compat.install()  # idempotent: no second warning
        print(len(caught), caught[0].category.__name__, 'chameleon.zpt.template' in str(caught[0].message))
        import chameleon as alias
        from chameleon.zpt.template import PageTemplate
        print(alias.__name__, PageTemplate is not stock, PageTemplate.__module__)
    """)
    assert result.stdout == '1 RuntimeWarning True\nchameleon_rs True chameleon_rs.template\n', result.stderr


def test_no_runtime_dependencies() -> None:
    # chameleon_rs imports the standard library only (zope.i18n is optional, as in
    # Chameleon). Every module of the package imports with third-party packages blocked,
    # except the framework integrations, which import their framework; and neither
    # `import chameleon_rs` nor `compat.install()` imports them.
    result = run_python("""
        import importlib, importlib.abc, importlib.util, pkgutil, sys, sysconfig
        stdlib = sysconfig.get_paths()['stdlib']
        FRAMEWORKS = {'chameleon_rs.flask', 'chameleon_rs.fastapi', 'chameleon_rs.starlette', 'chameleon_rs.robyn', 'chameleon_rs.litestar'}

        class Block(importlib.abc.MetaPathFinder):
            on = True
            def find_spec(self, name, path=None, target=None):
                top = name.split('.')[0]
                if not self.on or top in sys.stdlib_module_names or top == 'chameleon_rs':
                    return None
                raise ImportError(f'not the standard library: {name}')

        block = Block()
        sys.meta_path.insert(0, block)
        import chameleon_rs
        import chameleon_rs.compat
        chameleon_rs.compat.install()
        import chameleon_partials
        assert chameleon_partials is chameleon_rs.partials
        for info in pkgutil.walk_packages(chameleon_rs.__path__, 'chameleon_rs.'):
            if info.name not in FRAMEWORKS:
                importlib.import_module(info.name)
        imported = {'flask', 'werkzeug', 'quart', 'fastapi', 'starlette', 'pydantic', 'robyn', 'litestar', 'msgspec'} & set(sys.modules)
        assert not imported and not FRAMEWORKS & set(sys.modules), 'framework imported'
        for old in ('chameleon_flask', 'fastapi_chameleon', 'chameleon_robyn'):
            try:
                importlib.import_module(old)
            except ImportError as e:
                print('blocked:', e)
            sys.modules.pop(old, None)
        for module in FRAMEWORKS:
            sys.modules.pop(module, None)
        block.on = False
        # Robyn has no wheel for every Python we test (3.15, free-threaded).
        has_robyn = importlib.util.find_spec('robyn') is not None
        for module in FRAMEWORKS:
            if module != 'chameleon_rs.robyn' or has_robyn:
                importlib.import_module(module)
        import chameleon_flask, chameleon_flask.engine
        from chameleon_flask.exceptions import FlaskChameleonException
        assert chameleon_flask.engine is chameleon_flask is chameleon_rs.flask
        import fastapi_chameleon, fastapi_chameleon.engine
        from fastapi_chameleon.exceptions import FastAPIChameleonException
        assert fastapi_chameleon.engine is fastapi_chameleon is chameleon_rs.fastapi
        if has_robyn:
            import chameleon_robyn, chameleon_robyn.engine
            from chameleon_robyn.exceptions import ChameleonRobynException
            assert chameleon_robyn.engine is chameleon_robyn is chameleon_rs.robyn
        assert 'chameleon.starlette' not in sys.modules and 'chameleon.fastapi' not in sys.modules
        assert 'chameleon.litestar' not in sys.modules and 'litestar' in sys.modules
        print('ok')
    """)
    blocked = ''.join(f'blocked: not the standard library: {name}\n' for name in ('flask', 'fastapi', 'robyn'))
    assert result.stdout == blocked + 'ok\n', result.stderr


# --- custom types subclassing TalesExpr (z3c.pt's pattern) ----------------------------------


def lookup_type(engine: Any) -> Any:
    import ast

    class LookupExpr(engine.tales.TalesExpr):
        """`lookup: a | b`: the scope entry named, piped as TALES does."""

        def translate(self, expression: str, target: Any) -> Any:
            return engine.codegen.template(
                'target = econtext[key]', target=target, key=ast.Constant(expression.strip())
            )

    return LookupExpr


@pytest.mark.parametrize(
    'source',
    [
        '${lookup: a}',
        '${lookup: missing | a}',
        '${lookup: m1 | m2 | python: 7}',
        '${lookup: missing}',
        '<p tal:content="lookup: m | nothing"/>',
    ],
)
def test_custom_tales_expr_subclass(source: str) -> None:
    def render(engine: Any) -> Any:
        class Template(engine.PageTemplate):
            expression_types = {**engine.PageTemplate.expression_types, 'lookup': lookup_type(engine)}  # noqa: RUF012

        return lambda: Template(source)(a='A')

    assert outcome(render(chameleon_rs)) == outcome(render(chameleon))


# --- tal helpers (vendored) -----------------------------------------------------------------

TAL_CLAUSES = [
    'a 1',
    'global a 1; local b 2',
    '(a, b) x',
    'a 1;; b; c 2;',
    'a &amp; b; c d',
    'bad',
    '\xa0hello lovely',
    'text x',
    'structure  y',
    'title x; title y',
    'x',
    '',
]


@pytest.mark.parametrize('clause', TAL_CLAUSES)
@pytest.mark.parametrize('func', ['split_parts', 'parse_defines', 'parse_attributes', 'parse_substitution'])
def test_tal_helpers(func: str, clause: str) -> None:
    import chameleon.tal
    import chameleon_rs.tal

    def call(module: Any, token: Any) -> Any:
        def run() -> Any:
            result = getattr(module, func)(token(clause, 3, clause))
            return repr([(type(p).__name__, getattr(p, 'pos', None)) for p in result]) + repr(result)

        return run

    assert outcome(call(chameleon_rs.tal, chameleon_rs.tokenize.Token)) == outcome(
        call(chameleon.tal, chameleon.tokenize.Token)
    )


def test_tal_prepare_attributes() -> None:
    import chameleon.tal
    import chameleon_rs.tal

    attrs = [
        {'name': 'class', 'value': 'a', 'quote': '"', 'space': ' ', 'eq': '='},
        {'name': 'tal:x', 'value': 'b', 'quote': "'", 'space': '  ', 'eq': '='},
        {'name': 'Title', 'value': 't', 'quote': '"', 'space': ' ', 'eq': '='},
    ]
    ns = [('', 'a'), ('tal-ns', 'b'), ('', 't')]
    args = (attrs, [('title', 'expr'), ('new', 'e2'), (None, 'd')], ['alt', 'class'], ns, {'tal-ns'})
    assert chameleon_rs.tal.prepare_attributes(*args) == chameleon.tal.prepare_attributes(*args)


class UnimportableMarker:
    pass


@pytest.mark.parametrize('kind', ['instance', 'class', 'object'])
def test_default_marker_must_be_importable(kind: str) -> None:
    values = {'instance': UnimportableMarker(), 'class': UnimportableMarker, 'object': object()}

    def compile_with(engine: Any) -> Any:
        marker = engine.astutil.Symbol(values[kind])
        return lambda: engine.PageTemplate('<p tal:content="x">d</p>', default_marker=marker)(x=1)

    assert outcome(compile_with(chameleon_rs)) == outcome(compile_with(chameleon))


def test_expression_evaluator_evaluates_in_the_callers_scope() -> None:
    results = []
    for engine in (chameleon, chameleon_rs):
        parser = engine.tales.ExpressionParser({'python': engine.tales.PythonExpr}, 'python')
        factory = functools.partial(engine.compiler.ExpressionEngine, parser)
        evaluator = engine.compiler.ExpressionEvaluator(factory, {'foo': 'bar'})
        scope = engine.utils.Scope({'a': 1}).copy()
        value = evaluator(scope, {}, 'python', 'econtext.setdefault("q", a + 1)')
        results.append((value, sorted(dict(scope).items())))
    assert results[1] == results[0]


def test_concurrent_loads_share_one_template(tmp_path: Path) -> None:
    make_tree(tmp_path)
    loader = chameleon_rs.PageTemplateLoader(str(tmp_path / 'a'))
    barrier = threading.Barrier(16)
    found: list[Any] = []

    def load() -> None:
        barrier.wait()
        found.append(loader['page.pt'])

    threads = [threading.Thread(target=load) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(t) for t in found}) == 1
