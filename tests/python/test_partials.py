"""Built-in partials (D10, Phase 9): `render_partial(path, **kwargs)` in any template, rendered
as a native frame, compared with Chameleon + chameleon_partials (the reference): the output,
or the exception's type and whole formatted message."""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import chameleon
import chameleon_partials
import chameleon_rs
import chameleon_rs.partials
import pytest

ADDRESS = re.compile(r'0x[0-9a-fA-F]+')
# What `econtext['render_partial']` prints: chameleon_partials' function, or our renderer.
RENDERER = re.compile(r'<function render_partial at 0x\?>|<render_partial of <\w+ [^>]*>>|<render_partial>')


def canon(text: str) -> str:
    text = ADDRESS.sub('0x?', text)
    text = RENDERER.sub('<render_partial>', text)
    text = text.replace('chameleon_rs.partials.HTML', 'chameleon_partials.HTML')
    text = text.replace('<PartialRenderer (', '<function (')
    return text.replace('chameleon_rs.', 'chameleon.')


def write(folder: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def outcome(fn: Any) -> tuple[Any, ...]:
    try:
        return ('ok', canon(fn()))
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return ('raise', type(e).__name__, canon(str(e)))


@pytest.fixture
def folder(tmp_path: Path) -> Any:
    yield tmp_path
    chameleon_rs.partials.clear()
    chameleon_partials.has_registered_extensions = False


def compare(folder: Path, files: dict[str, str], page: str = 'page.pt', **model: Any) -> tuple[Any, ...]:
    """Renders `page` from `folder` with both engines: Chameleon with chameleon_partials'
    `render_partial` in the model; chameleon_rs with the built-in in the model (as an app
    using `extend_model` passes it), and unbound (the page's `econtext` then lacks it, which
    only an error message's argument list shows)."""
    write(folder, files)
    chameleon_partials.register_extensions(str(folder), cache_init=False)
    reference = chameleon.PageTemplateLoader(str(folder))[page]
    want = outcome(lambda: reference(render_partial=chameleon_partials.render_partial, **model))
    chameleon_rs.configure(template_folder=str(folder))
    ours = chameleon_rs.PageTemplateLoader(str(folder))[page]
    got = outcome(lambda: ours(render_partial=chameleon_rs.render_partial, **model))
    assert got == want
    unbound = outcome(lambda: ours(**model))
    if want[0] == 'ok':
        assert unbound == want
    else:
        assert unbound[:2] == want[:2]
        assert unbound[2].split(' - Arguments:')[0] == want[2].split(' - Arguments:')[0]
    return got


CARD = {'card.pt': '<b>${name}</b>'}

GRID: list[tuple[str, dict[str, str], dict[str, Any]]] = [
    ('basic', {'page.pt': '<p>${render_partial("card.pt", name="a<b")}</p>', **CARD}, {}),
    (
        'in a loop, with repeat on both sides',
        {
            'page.pt': '<p tal:repeat="n names">${repeat.n.index}${render_partial("item.pt", n=n)}${repeat.n.end}</p>',
            'item.pt': '<i tal:repeat="c n">${c}${repeat.c.index}</i>',
        },
        {'names': ['ab', 'c']},
    ),
    (
        'nested partials',
        {
            'page.pt': '<div>${render_partial("shared/outer.pt", v=1)}</div>',
            'shared/outer.pt': '<section>${render_partial("shared/inner.pt", v=v + 1)}</section>',
            'shared/inner.pt': '<span>${v}</span>',
        },
        {},
    ),
    (
        'attribute, content, replace',
        {
            'page.pt': '<a title="${render_partial(\'card.pt\', name=1)}" tal:content="render_partial(\'card.pt\', name=2)"/>'
            '<tal:r replace="render_partial(\'card.pt\', name=3)"/>',
            **CARD,
        },
        {},
    ),
    (
        'not a whole expression: called as Python',
        {
            'page.pt': '<p tal:define="h render_partial(\'card.pt\', name=4)">${h}${render_partial("card.pt", name=5) if 1 else 0}</p>',
            **CARD,
        },
        {},
    ),
    (
        'fresh scope: no outer names',
        {'page.pt': '<p tal:define="secret 1">${render_partial("leak.pt")}</p>', 'leak.pt': '${secret | "hidden"}'},
        {'other': 2},
    ),
    (
        'kwargs shadowing builtins and defaults',
        {
            'page.pt': '${render_partial("shadow.pt", repeat=7, target_language="fr", nothing=1, template="t")}',
            'shadow.pt': '${repeat}|${target_language}|${nothing}|${template}',
        },
        {},
    ),
    (
        'econtext of a partial',
        {
            'page.pt': '${render_partial("ctx.pt", a=1)}',
            'ctx.pt': '${", ".join(k for k in econtext if not k.startswith("__"))}|${target_language}',
        },
        {},
    ),
    (
        'global define does not leak into the page',
        {
            'page.pt': '${render_partial("g.pt")}<p metal:use-macro="load: m.pt"/>${x | "none"}',
            'g.pt': '<i tal:define="global x 5">${x}</i>',
            'm.pt': '<b>m</b>',
        },
        {},
    ),
    (
        'partial in a macro and in a slot',
        {
            'page.pt': '<html metal:use-macro="load: layout.pt"><div metal:fill-slot="body">'
            '${render_partial("card.pt", name="slot")}</div></html>',
            'layout.pt': '<html><body>${render_partial("card.pt", name="layout")}'
            '<div metal:define-slot="body"/></body></html>',
            **CARD,
        },
        {},
    ),
    (
        'partial using a macro of its own',
        {
            'page.pt': '${render_partial("usem.pt", v=3)}',
            'usem.pt': '<div metal:define-macro="m">[${v}]</div><p metal:use-macro="template.macros[\'m\']"/>',
        },
        {},
    ),
    (
        'i18n around and inside',
        {
            'page.pt': '<p i18n:translate="">Hi <b i18n:name="who">${render_partial("t.pt", n=1)}</b></p>',
            't.pt': '<i i18n:translate="">x ${n}</i>',
        },
        {},
    ),
    (
        'bytes decoded as utf-8',
        {'page.pt': '${render_partial("b.pt", b="é".encode())}', 'b.pt': '${b}'},
        {},
    ),
    (
        'explicit render_partial argument',
        {
            'page.pt': '${render_partial("rp.pt", render_partial=None)}',
            'rp.pt': '${render_partial is None}',
        },
        {},
    ),
    ('missing partial', {'page.pt': '<p>\n${render_partial("nope.pt", a=1)}</p>'}, {}),
    (
        'error in a partial',
        {'page.pt': '<p tal:define="q 1">${render_partial("bad.pt", a=[1])}</p>', 'bad.pt': '<i>\n  ${a[5]}</i>'},
        {'z': 'Z'},
    ),
    (
        'error in a nested partial after a define',
        {
            'page.pt': '<p>${render_partial("mid.pt", a=1)}</p>',
            'mid.pt': '<i tal:define="d 2">${render_partial("bad.pt", d=d)}</i>',
            'bad.pt': '<u tal:define="e 3">${1 / 0}</u>',
        },
        {},
    ),
    (
        'error in a partial caught by on-error',
        {
            'page.pt': '<p tal:on-error="string:E${error.type.__name__}">${render_partial("bad.pt")}</p>|${rcontext}',
            'bad.pt': '${1 / 0}',
        },
        {},
    ),
    (
        'error in a partial called from a macro',
        {
            'page.pt': '<html metal:use-macro="load: layout.pt"/>',
            'layout.pt': '<body>${render_partial("bad.pt", k="v")}</body>',
            'bad.pt': '${undefined_name}',
        },
        {},
    ),
    (
        'on-error inside the partial',
        {
            'page.pt': '${render_partial("oe.pt")}',
            'oe.pt': '<p tal:on-error="string:caught">${1/0}</p>',
        },
        {},
    ),
]


@pytest.mark.parametrize(('files', 'model'), [(f, m) for _, f, m in GRID], ids=[name for name, _, _ in GRID])
def test_partials_match_chameleon_partials(folder: Path, files: dict[str, str], model: dict[str, Any]) -> None:
    compare(folder, files, **model)


def test_partials_resolve_relative_to_the_calling_template(tmp_path: Path) -> None:
    # No configuration: the caller's folder, then each folder above it.
    write(
        tmp_path,
        {
            'home/index.pt': '${render_partial("_near.pt")}|${render_partial("shared/far.pt", x=1)}',
            'home/_near.pt': 'near',
            'shared/far.pt': 'far${x}${render_partial("_sib.pt")}',
            'shared/_sib.pt': 'sib',
        },
    )
    page = chameleon_rs.PageTemplateFile(str(tmp_path / 'home' / 'index.pt'))
    assert page() == 'near|far1sib'


def test_configured_folder_comes_first(folder: Path) -> None:
    write(folder, {'conf/x.pt': 'configured', 'page/x.pt': 'relative', 'page/p.pt': '${render_partial("x.pt")}'})
    page = chameleon_rs.PageTemplateFile(str(folder / 'page' / 'p.pt'))
    assert page() == 'relative'
    chameleon_rs.configure(template_folder=str(folder / 'conf'))
    assert page() == 'configured'
    chameleon_rs.partials.clear()
    assert page() == 'relative'


def test_loader_search_path_comes_before_the_callers_folder(tmp_path: Path) -> None:
    write(tmp_path, {'a/home/p.pt': '${render_partial("x.pt")}', 'a/home/x.pt': 'own', 'a/x.pt': 'loader'})
    loader = chameleon_rs.PageTemplateLoader(str(tmp_path / 'a'))
    assert loader['home/p.pt']() == 'loader'


def test_string_templates_need_a_configured_folder(folder: Path) -> None:
    write(folder, CARD)
    with pytest.raises(ValueError, match=r'Template not found: card.pt\.'):
        chameleon_rs.PageTemplate('${render_partial("card.pt", name=1)}')()
    chameleon_rs.configure(template_folder=str(folder))
    assert chameleon_rs.PageTemplate('${render_partial("card.pt", name=1)}')() == '<b>1</b>'


def test_markup_round_trip_from_python(folder: Path) -> None:
    write(folder, {'card.pt': '<b>${name}</b>'})
    chameleon_partials.register_extensions(str(folder), cache_init=False)
    chameleon_rs.configure(template_folder=str(folder))
    ours = chameleon_rs.render_partial('card.pt', name='<x>')
    theirs = chameleon_partials.render_partial('card.pt', name='<x>')
    assert ours.html_text == theirs.html_text == '<b>&lt;x&gt;</b>'
    assert ours.__html__() == theirs.__html__()
    source = '<div>${h}</div><p tal:content="h"/><a title="${h}"/>'
    assert chameleon_rs.PageTemplate(source)(h=ours) == chameleon.PageTemplate(source)(h=theirs)


def test_python_call_without_configuration() -> None:
    chameleon_rs.partials.clear()
    with pytest.raises(chameleon_rs.partials.PartialsException, match='register_extensions'):
        chameleon_rs.render_partial('x.pt')
    assert issubclass(chameleon_rs.partials.PartialsException, chameleon_rs.web.TemplateError)


def test_compat_api(folder: Path) -> None:
    import chameleon_rs.partials as cp

    with pytest.raises(cp.PartialsException, match="must be a folder, it's not"):
        cp.register_extensions(str(folder / 'missing'), cache_init=False)
    with pytest.raises(cp.PartialsException, match='must be specified'):
        cp.register_extensions('', cache_init=False)
    write(folder, CARD)
    cp.register_extensions(str(folder))
    assert cp.has_registered_extensions and cp.template_path == str(folder)
    cp.register_extensions(str(folder / 'other'))  # cache_init: ignored
    assert cp.template_path == str(folder)
    assert cp.extend_model(None) == {'render_partial': cp.render_partial}
    with pytest.raises(cp.PartialsException):
        cp.extend_model([])  # type: ignore[arg-type]
    model = cp.extend_model({'name': 'n'})
    assert chameleon_rs.PageTemplate('${render_partial("card.pt", name=name)}')(**model) == '<b>n</b>'


def test_auto_reload_is_checked_once_per_render(folder: Path) -> None:
    write(folder, {'p.pt': '${render_partial("c.pt")}${render_partial("c.pt")}', 'c.pt': 'one'})
    chameleon_rs.configure(template_folder=str(folder), auto_reload=True)
    page = chameleon_rs.PageTemplateFile(str(folder / 'p.pt'))
    assert page() == 'oneone'
    target = chameleon_rs.partials.resolve(page, 'c.pt')
    calls = []
    original = type(target).cook_check

    def counting(self: Any) -> Any:
        calls.append(self)
        return original(self)

    type(target).cook_check = counting  # type: ignore[method-assign]
    try:
        assert page() == 'oneone'
    finally:
        type(target).cook_check = original  # type: ignore[method-assign]
    assert calls.count(target) == 1
    import os
    import time

    (folder / 'c.pt').write_text('two')
    later = time.time() + 5
    os.utime(folder / 'c.pt', (later, later))
    assert page() == 'twotwo'


def test_infinite_recursion_is_a_recursion_error(folder: Path) -> None:
    write(folder, {'loop.pt': '<b>${render_partial("loop.pt")}</b>'})
    page = chameleon_rs.PageTemplateFile(str(folder / 'loop.pt'))
    with pytest.raises(RecursionError) as info:
        page()
    assert '- Expression:' not in str(info.value)
    write(folder, {'catch.pt': '<p tal:on-error="string:deep">${render_partial("loop.pt")}</p>'})
    assert chameleon_rs.PageTemplateFile(str(folder / 'catch.pt'))() == '<p>deep</p>'


def test_partials_are_native_frames(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A whole `${render_partial(...)}` never calls the partial's `render` (or the renderer).
    write(folder, {'p.pt': '<i tal:repeat="n range(3)">${render_partial("c.pt", n=n)}</i>', 'c.pt': '${n}'})
    page = chameleon_rs.PageTemplateFile(str(folder / 'p.pt'))
    page()
    calls: list[Any] = []
    monkeypatch.setattr(chameleon_rs.PageTemplate, 'render', lambda *a, **k: calls.append(a))
    monkeypatch.setattr(chameleon_rs.partials.PartialRenderer, '__call__', lambda *a, **k: calls.append(a))
    from chameleon_rs._native import render_page

    assert render_page(page, None, {}) == '<i>0</i>\n<i>1</i>\n<i>2</i>'
    assert calls == []


def test_a_users_own_render_partial_is_called(folder: Path) -> None:
    write(folder, {'p.pt': '${render_partial("x", a=1)}'})
    page = chameleon_rs.PageTemplateFile(str(folder / 'p.pt'))
    assert page(render_partial=lambda name, **kw: f'<{name} {kw}>') == "&lt;x {'a': 1}&gt;"


LEGACY = """
    import sys, tempfile, pathlib
    import chameleon_rs.compat
    chameleon_rs.compat.install(integrations=False)
    sys.path.insert(0, {partials!r})
    import chameleon_partials, chameleon
    assert chameleon_partials.__file__.startswith({partials!r}), chameleon_partials.__file__
    folder = pathlib.Path(tempfile.mkdtemp())
    (folder / 'c.pt').write_text('<b>${{n}}</b>${{render_partial("d.pt")}}')
    (folder / 'd.pt').write_text('d')
    chameleon_partials.register_extensions(str(folder))
    page = chameleon.PageTemplate('<i tal:repeat="n range(2)">${{render_partial("c.pt", n=n)}}</i>')
    calls = []
    original = chameleon.PageTemplate.render
    chameleon.PageTemplate.render = lambda self, *a, **k: calls.append(1) or original(self, *a, **k)
    print(page(render_partial=chameleon_partials.render_partial), len(calls))
"""


def test_chameleon_partials_own_function_renders_natively() -> None:
    partials = str(Path(chameleon_partials.__file__).parent.parent)
    code = textwrap.dedent(LEGACY.format(partials=partials))
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False)
    # One `render` call: the page's own; both partial levels were native frames.
    assert result.stdout == '<i><b>0</b>d</i>\n<i><b>1</b>d</i> 1\n', result.stderr
