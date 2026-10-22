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
    chameleon_rs._settings.clear()
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
        'conversions with the utf-8 defaults',
        {
            'page.pt': '${render_partial("v.pt", vs=vs)}',
            'v.pt': '<i tal:repeat="v vs">${v}|<b tal:content="v"/>|<a title="${v}"/></i>',
        },
        {'vs': [1, 2.5, None, True, b'b\xc3\xa9', type('M', (), {'default': 'd ${x}', 'mapping': {'x': 1}})()]},
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


def test_registered_search_is_chameleon_partials_lookup(folder: Path) -> None:
    # `partial_search='registered'`: exactly chameleon_partials. Nothing registered is its
    # `PartialsException`; a file next to the caller but not in the folder is not found.
    write(folder, {'conf/x.pt': 'configured', 'page/x.pt': 'relative', 'page/p.pt': '${render_partial("x.pt")}'})
    page = chameleon_rs.PageTemplateFile(str(folder / 'page' / 'p.pt'))
    chameleon_rs.configure(partial_search='registered')
    with pytest.raises(chameleon_rs.partials.PartialsException, match='register_extensions'):
        page()
    chameleon_rs.configure(template_folder=str(folder))
    with pytest.raises(ValueError, match=r'Template not found: x.pt\.'):
        page()
    chameleon_rs.configure(template_folder=str(folder / 'conf'))
    assert page() == 'configured'


@pytest.mark.parametrize('mode', ['caller', 'registered'])
def test_names_outside_the_registered_folder_are_not_found(folder: Path, mode: str) -> None:
    # Security, as chameleon_partials from 0.2.1: no `..` or absolute path out of the folder,
    # no package spec. A `..` that stays inside is fine.
    root = folder / 'templates'
    write(folder, {'secret.pt': 'SECRET'})
    files = {'page.pt': '<p>${render_partial(name)}</p>', 'shared/card.pt': 'card'}
    write(root, files)
    hostile = ['../secret.pt', 'shared/../../secret.pt', str(folder / 'secret.pt'), 'chameleon_rs:py.typed']
    for name in hostile:
        got = compare(root, files, name=name)
        assert got[:2] == ('raise', 'ValueError'), name
        assert got[2].startswith(f'Template not found: {name}.'), got
    assert compare(root, files, name='shared/../shared/card.pt') == ('ok', '<p>card</p>')
    chameleon_rs.configure(partial_search=mode)
    page = chameleon_rs.PageTemplateFile(str(root / 'page.pt'))
    for name in hostile:
        with pytest.raises(ValueError, match='Template not found'):
            chameleon_rs.render_partial(name)
        with pytest.raises(ValueError, match='Template not found'):
            page(name=name)
        # chameleon_partials' own function, rendering natively here: its check, not bypassed.
        with pytest.raises(ValueError, match='Template not found'):
            page(name=name, render_partial=chameleon_partials.render_partial)


def test_caller_search_is_the_default(folder: Path) -> None:
    # Owner-approved: where chameleon_partials raises (not registered, or not in the folder), the
    # caller's folders are searched; nothing that rendered there renders differently.
    write(
        folder,
        {
            'home/index.pt': '${render_partial("_near.pt")}|${render_partial("shared/far.pt", x=1)}',
            'home/_near.pt': 'near',
            'shared/far.pt': 'far${x}${render_partial("_sib.pt")}',
            'shared/_sib.pt': 'sib',
        },
    )
    page = chameleon_rs.PageTemplateLoader(str(folder))['home/index.pt']
    assert page() == 'near|far1sib'
    chameleon_rs.configure(partial_search='caller')
    assert page() == 'near|far1sib'
    with pytest.raises(ValueError, match='partial_search'):
        chameleon_rs.configure(partial_search='everywhere')


def test_caller_search_never_leaves_the_template_tree(folder: Path) -> None:
    # Security: the upward walk stops at the root that holds the caller (the configured
    # folder or a search path entry), so a file planted above it never compiles.
    write(
        folder,
        {
            'widgets/nav.pt': 'PLANTED',
            'secret.txt': 'SECRET',
            'app/templates/home/index.pt': '${render_partial(name)}',
            'app/templates/home/_near.pt': 'near',
            'app/templates/shared/far.pt': 'far',
        },
    )
    root = folder / 'app' / 'templates'
    pages = [
        chameleon_rs.PageTemplateLoader(str(root))['home/index.pt'],
        chameleon_rs.PageTemplateFile(str(root / 'home' / 'index.pt')),
    ]
    chameleon_rs.configure(template_folder=str(root))
    for page in pages:
        assert page(name='_near.pt') == 'near'
        assert page(name='shared/far.pt') == 'far'
    chameleon_rs._settings.clear()
    hosts = [n for n in ('etc/hosts', 'private/etc/hosts') if Path('/', n).is_file()]
    hostile = [
        'widgets/nav.pt',
        'secret.txt',
        '../../../secret.txt',
        '../shared/../../../secret.txt',
        str(folder / 'secret.txt'),
        *hosts,
    ]
    for page in pages:
        for name in hostile:
            with pytest.raises(ValueError, match='Template not found'):
                page(name=name)
    # In no root (a bare file, nothing configured): its own folder only.
    bare = chameleon_rs.PageTemplateFile(str(root / 'home' / 'index.pt'))
    assert bare(name='_near.pt') == 'near'
    for name in ['shared/far.pt', '../shared/far.pt', *hostile]:
        with pytest.raises(ValueError, match='Template not found'):
            bare(name=name)
    # `..` inside the tree is still fine.
    assert pages[0](name='../shared/far.pt') == 'far'


def test_with_nothing_configured_the_walk_stops_at_the_packages_templates_folder(folder: Path) -> None:
    # A Pyramid renderer: a bare file, no configured folder, no search path. The walk climbs to
    # `templates` only when its parent is a package, never past it.
    write(
        folder,
        {
            'outside.pt': 'PLANTED',
            'app/__init__.py': '',
            'app/widgets.pt': 'PLANTED',
            'app/templates/home/index.pt': '${render_partial(name)}',
            'app/templates/home/_near.pt': 'near',
            'app/templates/shared/far.pt': 'far',
            'loose/templates/home/index.pt': '${render_partial(name)}',
            'loose/templates/shared/far.pt': 'far',
            'deep/__init__.py': '',
            'deep/web/templates/home/index.pt': '${render_partial(name)}',
            'deep/web/templates/shared/far.pt': 'far',
        },
    )
    page = chameleon_rs.PageTemplateFile(str(folder / 'app' / 'templates' / 'home' / 'index.pt'))
    assert page(name='_near.pt') == 'near'
    assert page(name='shared/far.pt') == 'far'
    assert page(name='../shared/far.pt') == 'far'
    for name in ['widgets.pt', '../widgets.pt', '../../widgets.pt', 'outside.pt', '../../../outside.pt']:
        with pytest.raises(ValueError, match='Template not found'):
            page(name=name)
    # `templates` not in a package (here, or with a non-package folder between): own folder only.
    for tree in ('loose', 'deep/web'):
        bare = chameleon_rs.PageTemplateFile(str(folder / tree / 'templates' / 'home' / 'index.pt'))
        with pytest.raises(ValueError, match='Template not found'):
            bare(name='shared/far.pt')


def test_an_integrations_folder_bounds_the_walk_whatever_its_name(folder: Path) -> None:
    # A folder configured for one integration only (not for partials, not named `templates`,
    # not in a package) is the root of a bare template inside it.
    write(
        folder,
        {
            'secret.pt': 'PLANTED',
            'site/views/home/index.pt': '${render_partial(name)}',
            'site/views/shared/far.pt': 'far',
        },
    )
    page = chameleon_rs.PageTemplateFile(str(folder / 'site' / 'views' / 'home' / 'index.pt'))
    with pytest.raises(ValueError, match='Template not found'):
        page(name='shared/far.pt')
    settings = chameleon_rs._settings.Settings(str(folder / 'site' / 'views'), False, True)
    chameleon_rs._settings.install(views_settings={'starlette': settings})
    assert chameleon_rs._settings.partials is None
    assert page(name='shared/far.pt') == 'far'
    for name in ['secret.pt', '../../secret.pt', '../../../secret.pt']:
        with pytest.raises(ValueError, match='Template not found'):
            page(name=name)


def test_with_caller_search_the_configured_folder_comes_first(folder: Path) -> None:
    write(folder, {'conf/x.pt': 'configured', 'page/x.pt': 'relative', 'page/p.pt': '${render_partial("x.pt")}'})
    page = chameleon_rs.PageTemplateFile(str(folder / 'page' / 'p.pt'))
    chameleon_rs.configure(partial_search='caller')
    assert page() == 'relative'
    chameleon_rs.configure(template_folder=str(folder / 'conf'))
    assert page() == 'configured'
    chameleon_rs.partials.clear()
    assert page() == 'relative'


def test_with_caller_search_the_loader_path_comes_before_the_callers_folder(folder: Path) -> None:
    write(folder, {'a/home/p.pt': '${render_partial("x.pt")}', 'a/home/x.pt': 'own', 'a/x.pt': 'loader'})
    chameleon_rs.configure(partial_search='caller')
    loader = chameleon_rs.PageTemplateLoader(str(folder / 'a'))
    assert loader['home/p.pt']() == 'loader'


def test_string_templates_use_the_configured_folder(folder: Path) -> None:
    write(folder, CARD)
    chameleon_rs.configure(partial_search='caller')
    with pytest.raises(ValueError, match=r'Template not found: card.pt\.'):
        chameleon_rs.PageTemplate('${render_partial("card.pt", name=1)}')()
    chameleon_rs.configure(template_folder=str(folder))
    assert chameleon_rs.PageTemplate('${render_partial("card.pt", name=1)}')() == '<b>1</b>'


def test_the_global_render_partial_in_a_model_resolves_as_called(folder: Path) -> None:
    # Passed in a model (`extend_model`), the global renderer has no template: even with caller
    # search (the default), a native call does not search the caller's folders (calling it
    # would not).
    write(folder, {'p.pt': '${render_partial("card.pt", name=1)}', 'card.pt': '<b>${name}</b>'})
    chameleon_rs.configure(partial_search='caller')
    page = chameleon_rs.PageTemplateFile(str(folder / 'p.pt'))
    assert page() == '<b>1</b>'
    with pytest.raises(chameleon_rs.partials.PartialsException, match='register_extensions'):
        page(render_partial=chameleon_rs.render_partial)


SETUP_ORDERS = ['partials then flask', 'flask then partials', 'configure']


@pytest.mark.parametrize('order', SETUP_ORDERS)
def test_setup_calls_configure_their_own_consumer(folder: Path, order: str) -> None:
    # chameleon_partials' and chameleon-flask's setup calls each configure their own library:
    # one does not make the other's (cache_init) a no-op, nor share its folder or options.
    flask_module = pytest.importorskip('chameleon_rs.flask')
    write(
        folder,
        {
            'p/alp.pt': '<div :class="x">p</div>',
            'p/page.pt': '${render_partial("alp.pt")}',
            'f/alp.pt': '<div :class="x">f</div>',
            'f/page.pt': '${render_partial("alp.pt")}',
        },
    )
    if order == 'partials then flask':
        chameleon_rs.partials.register_extensions(str(folder / 'p'), cache_init=False)
        flask_module.global_init(str(folder / 'f'), restricted_namespace=False)
    elif order == 'flask then partials':
        flask_module.global_init(str(folder / 'f'), restricted_namespace=False, cache_init=False)
        chameleon_rs.partials.register_extensions(str(folder / 'p'))
    else:
        chameleon_rs.configure(template_folder=str(folder / 'p'))
        flask_module.global_init(str(folder / 'f'), restricted_namespace=False)
        assert flask_module.template_path == str(folder / 'p')  # global_init ignored
        flask_module.clear()
        flask_module.global_init(str(folder / 'f'), restricted_namespace=False)
    try:
        # Flask renders from its folder with its options; partials from theirs, without them
        # (a `:class` attribute is an error there, as with chameleon_partials' own loader).
        assert flask_module.render('alp.pt') == '<div :class="x">f</div>'
        with pytest.raises(KeyError, match='Undefined namespace prefix'):
            flask_module.render('page.pt')
        assert chameleon_rs.partials.template_path == str(folder / 'p')
    finally:
        flask_module.clear()


def test_flask_render_needs_flask_setup(folder: Path) -> None:
    flask_module = pytest.importorskip('chameleon_rs.flask')
    write(folder, CARD)
    flask_module.clear()
    chameleon_rs.partials.register_extensions(str(folder), cache_init=False)
    with pytest.raises(flask_module.FlaskChameleonException, match='global_init'):
        flask_module.render('card.pt', name=1)


def test_translate_and_encoding_keywords_go_to_the_render(folder: Path) -> None:
    # chameleon_partials passes them to `render`, where they are its arguments: the partial is
    # rendered through Python.
    write(folder, {'tr.pt': '<span i18n:translate="">Hello</span>', 'card.pt': '<b>${name}</b>'})
    chameleon_rs.configure(template_folder=str(folder))
    page = chameleon_rs.PageTemplate('${render_partial("tr.pt", translate=tx)}')
    assert page(tx=lambda msgid, **kw: f'T({msgid})') == '<span>T(Hello)</span>'
    page = chameleon_rs.PageTemplate('${render_partial("card.pt", name="a", encoding="latin-1")}')
    with pytest.raises(TypeError, match="multiple values for keyword argument 'encoding'"):
        page()


def test_a_partial_uses_its_own_default_marker(folder: Path) -> None:
    from chameleon_rs.astutil import Symbol

    write(folder, {'m.pt': '<a title="${t}" tal:attributes="href default">x</a>'})
    chameleon_rs.configure(template_folder=str(folder))
    marker = chameleon_rs.partials  # an importable object
    page = chameleon_rs.PageTemplate('${render_partial("m.pt", t=1)}', default_marker=Symbol(marker))
    assert page() == chameleon_rs.PageTemplateFile(str(folder / 'm.pt'))(t=1)


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
        cp.extend_model([])  # ty: ignore[invalid-argument-type] - the wrong type on purpose
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
    chameleon_rs.configure(template_folder=str(folder))
    page = chameleon_rs.PageTemplateFile(str(folder / 'loop.pt'))
    with pytest.raises(RecursionError) as info:
        page()
    assert '- Expression:' not in str(info.value)
    write(folder, {'catch.pt': '<p tal:on-error="string:deep">${render_partial("loop.pt")}</p>'})
    assert chameleon_rs.PageTemplateFile(str(folder / 'catch.pt'))() == '<p>deep</p>'


def test_partials_are_native_frames(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A whole `${render_partial(...)}` never calls the partial's `render` (or the renderer).
    write(folder, {'p.pt': '<i tal:repeat="n range(3)">${render_partial("c.pt", n=n)}</i>', 'c.pt': '${n}'})
    chameleon_rs.configure(template_folder=str(folder))
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


def test_every_spelling_of_a_partial_is_one_template(folder: Path) -> None:
    # Security: each spelling once compiled and cached its own copy (memory without bound).
    write(folder, {'page.pt': '${render_partial(name)}', 'shared/card.pt': 'card'})
    chameleon_rs.configure(template_folder=str(folder))
    page = chameleon_rs.PageTemplateFile(str(folder / 'page.pt'))
    spellings = [
        'shared/card.pt',
        './shared/card.pt',
        'shared//card.pt',
        'shared/./card.pt',
        'shared/../shared/card.pt',
    ]
    for name in spellings:
        assert page(name=name) == 'card'
    settings = chameleon_rs._settings.partials
    assert settings is not None
    assert len(settings.loader.registry) == 1
    assert len(page.__dict__['_partial_cache']) == 1
