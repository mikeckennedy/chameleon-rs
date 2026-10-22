"""The `markdown:` expression type (Phase 11b): `configure(markdown=...)`, each profile option,
the escaping default, fallbacks and empty values, macros, `render_async`, the cache, and
concurrent renders. The CommonMark and GFM spec examples run in Rust
(`crates/chameleon-core/tests/markdown_spec.rs`)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

import chameleon_rs
import chameleon_rs._settings
import pytest
from chameleon_rs import PageTemplate, PageTemplateLoader
from chameleon_rs._native import markdown_cache_info
from chameleon_rs.utils import Markup


@pytest.fixture(autouse=True)
def reset() -> Any:
    chameleon_rs._settings.clear()
    yield
    chameleon_rs._settings.clear()


def md(source: str, profiles: Any = True, **model: Any) -> str:
    chameleon_rs.configure(markdown=profiles)
    return PageTemplate(source)(**model)


# -- the expression ------------------------------------------------------------------------


def test_not_configured_is_an_unknown_type() -> None:
    with pytest.raises(LookupError, match="Unknown expression type: 'markdown'"):
        PageTemplate('${markdown: x}')(x='a')


def test_interpolation_and_content() -> None:
    out = md('<div>${markdown: body}</div><p tal:content="markdown: body"/>', body='Hi *there*')
    assert out == '<div><p>Hi <em>there</em></p>\n</div><p><p>Hi <em>there</em></p>\n</p>'


def test_commonmark_and_gfm() -> None:
    body = '# Title\n\n~~old~~ https://example.com\n\n- [x] done\n\n| a |\n|---|\n| 1 |\n\nNote[^1]\n\n[^1]: Here.\n'
    out = md('${markdown: body}', body=body)
    assert '<h1>Title</h1>' in out
    assert '<del>old</del>' in out
    assert '<a href="https://example.com">https://example.com</a>' in out
    assert 'type="checkbox"' in out
    assert '<table>' in out
    assert 'footnote' in out


def test_fenced_code_gets_a_language_class() -> None:
    out = md('${markdown: body}', body='```python\nprint(1 < 2)\n```')
    assert out == '<pre><code class="language-python">print(1 &lt; 2)\n</code></pre>\n'


def test_raw_html_is_escaped_by_default() -> None:
    out = md('${markdown: body}', body='<script>alert(1)</script> and <b>bold</b>')
    assert '<script>' not in out
    assert '&lt;script&gt;' in out
    assert '&lt;b&gt;bold&lt;/b&gt;' in out


def test_the_result_is_not_escaped_again() -> None:
    assert md('<div>${markdown: t}</div>', t='a & b') == '<div><p>a &amp; b</p>\n</div>'


ATTRIBUTE_SITES = [
    '<img alt="${md: x}"/>',
    "<img alt='${md: x}'/>",
    '<img alt="a ${md: x} b"/>',
    '<img tal:attributes="alt md: x"/>',
    '<img tal:define="h md: x" alt="${h}"/>',
    '<img tal:define="h md: x" tal:attributes="alt h"/>',
    '<img tal:attributes="d"/>',
]


@pytest.mark.parametrize('source', ATTRIBUTE_SITES)
def test_in_an_attribute_the_result_is_escaped(source: str) -> None:
    # Security: Markdown's own `href="..."` would close the attribute, and the text could add
    # an event handler (`onmouseover=...`) to the element.
    x = '[a](onmouseover=alert(1)//)'
    out = md(source, {'md': {'inline': True}}, x=x, d={'alt': chameleon_rs.utils.MarkdownMarkup('<a href="x">')})
    tags = _tags(out)
    assert [name for name, _ in tags] == ['img'], out
    assert [name for name, _ in tags[0][1]] == ['alt'], out
    assert '<a href=' in (tags[0][1][0][1] or ''), out


def _tags(html: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    from html.parser import HTMLParser

    found: list[tuple[str, list[tuple[str, str | None]]]] = []

    class Parser(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            found.append((tag, attrs))

    Parser().feed(html)
    return found


def test_structure_keeps_markdown_raw_in_an_attribute() -> None:
    # `structure` is the template author's explicit opt-in, as for any value.
    out = md('<img alt="${structure: md: x}"/>', {'md': {'inline': True}}, x='*a*')
    assert out == '<img alt="<em>a</em>"/>'


def test_unsafe_urls_are_dropped_unless_raw_html_is_allowed() -> None:
    assert md('${markdown: s}', s='[x](javascript:alert(1))') == '<p><a href="">x</a></p>\n'
    allowed = md('${md: s}', {'md': {'raw_html': 'allow'}}, s='[x](javascript:alert(1))')
    assert allowed == '<p><a href="javascript:alert(1)">x</a></p>\n'


def test_default_keeps_the_element_content() -> None:
    assert md('<p tal:content="markdown: missing | default">fallback</p>') == '<p>fallback</p>'


def test_lone_surrogates_are_replaced() -> None:
    assert md('${markdown: s}', s='a\ud800b') == '<p>a\ufffdb</p>\n'


def test_none_and_empty_render_nothing() -> None:
    assert md('<i>${markdown: a}|${markdown: b}</i>', a=None, b='') == '<i>|</i>'
    assert md('<i tal:content="markdown: a">x</i>', a=None) == '<i></i>'


def test_other_values_go_through_str() -> None:
    class Note:
        def __str__(self) -> str:
            return '**bold**'

    assert md('${markdown: n}', n=Note()) == '<p><strong>bold</strong></p>\n'
    assert md('${markdown: n}', n=42) == '<p>42</p>\n'


def test_markup_input_converts_too() -> None:
    assert md('${markdown: m}', m=Markup('*x*')) == '<p><em>x</em></p>\n'


def test_pipe_fallbacks() -> None:
    class Page:
        body = '*real*'

    source = "${markdown: page.body | 'Coming *soon*'}"
    assert md(source, page=Page()) == '<p><em>real</em></p>\n'
    assert md(source, page=object()) == '<p>Coming <em>soon</em></p>\n'
    # A pipe falls back on an error, not on None (as everywhere in TALES).
    assert md("${markdown: v | 'x'}", v=None) == ''


def test_the_inner_expression_may_have_a_prefix() -> None:
    assert md('${markdown: string:*${name}*}', name='Ada') == '<p><em>Ada</em></p>\n'


def test_in_macros_and_slots() -> None:
    chameleon_rs.configure(markdown=True)
    layout = PageTemplate('<div metal:define-macro="m"><h1>${markdown: title}</h1><main metal:define-slot="s"/></div>')
    page = PageTemplate(
        '<div metal:use-macro="layout.macros[\'m\']"><main metal:fill-slot="s">${markdown: body}</main></div>'
    )
    out = page(layout=layout, title='*T*', body='- a')
    assert out == '<div><h1><p><em>T</em></p>\n</h1><main><ul>\n<li>a</li>\n</ul>\n</main></div>'


def test_render_async() -> None:
    chameleon_rs.configure(markdown=True)
    page = PageTemplate('<h1>${title}</h1><div>${markdown: body}</div>')

    async def title() -> str:
        await asyncio.sleep(0)
        return 'T'

    async def main() -> str:
        return await page.render_async(title=title(), body='*plain*')

    assert asyncio.run(main()) == '<h1>T</h1><div><p><em>plain</em></p>\n</div>'


def test_render_async_awaits_only_final_values() -> None:
    # As for `structure:` (D9): a value computed inside the expression isn't awaited, so the
    # Markdown text has to be loaded before the render (in the view).
    chameleon_rs.configure(markdown=True)

    async def body() -> str:
        return '*x*'

    async def main() -> str:
        coroutine = body()
        try:
            return await PageTemplate('${markdown: body}').render_async(body=coroutine)
        finally:
            coroutine.close()

    assert asyncio.run(main()).startswith('<p>&lt;coroutine object')


def test_file_templates(tmp_path: Any) -> None:
    (tmp_path / 'page.pt').write_text('<article>${markdown: body}</article>')
    chameleon_rs.configure(markdown=True)
    page = PageTemplateLoader([str(tmp_path)])['page.pt']
    assert page(body='# Hi') == '<article><h1>Hi</h1>\n</article>'


# -- profiles and options -------------------------------------------------------------------


def test_profiles_are_their_own_prefixes() -> None:
    profiles = {'markdown': True, 'md-comment': {'images': False}}
    out = md('${markdown: s}|${md-comment: s}', profiles, s='![alt *text*](http://x/i.png)')
    assert out == '<p><img src="http://x/i.png" alt="alt text" /></p>\n|<p>alt text</p>\n'


def test_raw_html_allow() -> None:
    out = md('${md: s}', {'md': {'raw_html': 'allow'}}, s='<b>bold</b>')
    assert out == '<p><b>bold</b></p>\n'


def test_inline() -> None:
    profiles = {'md': {'inline': True}}
    assert md('<h1>${md: t}</h1>', profiles, t='The *Title*') == '<h1>The <em>Title</em></h1>'
    assert md('${md: t}', profiles, t='one\n\ntwo') == '<p>one</p>\n<p>two</p>\n'


def test_heading_ids() -> None:
    plain = md('${md: s}', {'md': {'heading_ids': True}}, s='## A Title')
    assert plain.startswith('<h2 id="a-title">A Title<a href="#a-title"')
    prefixed = md('${md: s}', {'md': {'heading_ids': 'doc-'}}, s='## A Title')
    assert prefixed.startswith('<h2 id="doc-a-title">A Title<a href="#doc-a-title"')
    assert md('${md: s}', {'md': {}}, s='## A Title') == '<h2>A Title</h2>\n'


def test_classes() -> None:
    classes = {'table': 'table is-striped', 'pre': 'code', 'h2': 'title "x"', 'hr': 'rule', 'img': 'pic'}
    body = '## T\n\n---\n\n```\nx\n```\n\n| a |\n|---|\n| 1 |\n\n![a](b.png)\n'
    out = md('${md: s}', {'md': {'classes': classes}}, s=body)
    assert '<h2 class="title &quot;x&quot;">' in out
    assert '<hr class="rule" />' in out
    assert '<pre class="code"><code>' in out
    assert '<table class="table is-striped">' in out
    assert '<img src="b.png" alt="a" class="pic" />' in out


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        # A paragraph in a tight list writes no <p>: its class goes nowhere.
        ('- a\n- b', '<ul>\n<li>a</li>\n<li>b</li>\n</ul>\n'),
        ('- one *two*', '<ul>\n<li>one <em>two</em></li>\n</ul>\n'),
        ('- <b>x</b>', '<ul>\n<li><b>x</b></li>\n</ul>\n'),
        ('- [ ] todo', '<ul>\n<li><input type="checkbox" disabled="" /> todo</li>\n</ul>\n'),
        # In a loose list it does.
        ('- a\n\n- b', '<ul>\n<li>\n<p class="text">a</p>\n</li>\n<li>\n<p class="text">b</p>\n</li>\n</ul>\n'),
    ],
)
def test_classes_in_lists(source: str, expected: str) -> None:
    profiles = {'md': {'raw_html': 'allow', 'classes': {'p': 'text'}}}
    assert md('${md: s}', profiles, s=source) == expected


def test_classes_on_list_elements() -> None:
    classes = {'ul': 'u', 'ol': 'o', 'li': 'i', 'a': 'l', 'code': 'c', 'blockquote': 'q', 'img': 'm'}
    out = md('${md: s}', {'md': {'classes': classes}}, s='- `x`\n\n3. [![a](b.png)](http://l)\n\n> q')
    assert '<ul class="u">\n<li class="i"><code class="c">x</code></li>' in out
    assert (
        '<ol start="3" class="o">\n<li class="i"><a href="http://l" class="l"><img src="b.png" alt="a" class="m" /></a>'
        in out
    )
    assert '<blockquote class="q">' in out


def test_classes_with_inline_and_without_images() -> None:
    profiles = {'md': {'inline': True, 'images': False, 'classes': {'p': 'text', 'img': 'pic'}}}
    assert md('${md: s}', profiles, s='a ![b](c.png)') == 'a b'


def test_classes_skip_raw_html() -> None:
    profiles = {'md': {'raw_html': 'allow', 'classes': {'p': 'text'}}}
    assert md('${md: s}', profiles, s='hi\n\n<p>raw</p>') == '<p class="text">hi</p>\n<p>raw</p>\n'


def test_extensions_switch_off() -> None:
    profiles = {'md': {'extensions': {'strikethrough': False, 'table': False, 'autolink': False}}}
    out = md('${md: s}', profiles, s='~~a~~ www.example.com\n\n| a |\n|---|')
    assert '<del>' not in out
    assert '<a ' not in out
    assert '<table>' not in out


@pytest.mark.parametrize(
    ('markdown', 'error', 'message'),
    [
        ('yes', TypeError, 'markdown must be True, False or a dict'),
        ({'string': True}, ValueError, "'string' is one of Chameleon's own expression types"),
        ({'Bad': True}, ValueError, 'profile names are expression prefixes'),
        ({'md': 1}, TypeError, "the options for 'md' must be a dict or True"),
        ({'md': {'oops': 1}}, ValueError, "unknown option 'oops' for 'md'"),
        ({'md': {'raw_html': 'yes'}}, ValueError, "raw_html for 'md' must be 'escape' or 'allow'"),
        ({'md': {'images': 'no'}}, TypeError, "images for 'md' must be True or False"),
        ({'md': {'heading_ids': 3}}, TypeError, "heading_ids for 'md' must be True, False or a prefix"),
        ({'md': {'heading_ids': '"><x>'}}, ValueError, "heading_ids for 'md' must be letters, digits"),
        ({'md': {'classes': {'div': 'x'}}}, ValueError, "unknown element 'div'"),
        ({'md': {'classes': ['x']}}, TypeError, "classes for 'md' must map element names"),
        ({'md': {'extensions': ['table']}}, TypeError, "extensions for 'md' must be a dict"),
        ({'md': {'extensions': {'tables': False}}}, ValueError, "extensions for 'md' must map some of"),
        ({'md': {'extensions': {'table': 'no'}}}, TypeError, "extensions\\['table'\\] for 'md' must be True or False"),
    ],
)
def test_invalid_settings(markdown: Any, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        chameleon_rs.configure(markdown=markdown)


def test_an_invalid_call_changes_nothing(tmp_path: Any) -> None:
    chameleon_rs.configure(template_folder=str(tmp_path), markdown={'md': True})
    with pytest.raises(ValueError):
        chameleon_rs.configure(partial_search='registered', markdown={'other': {'oops': 1}})  # ty: ignore[invalid-argument-type, invalid-key]
    assert list(chameleon_rs._settings.markdown_profiles) == ['md']
    assert chameleon_rs._settings.search_mode == 'caller'
    with pytest.raises(ValueError):
        chameleon_rs.configure(markdown={'other': True}, partial_search='nowhere')
    assert list(chameleon_rs._settings.markdown_profiles) == ['md']


def test_markdown_needs_no_template_folder() -> None:
    assert chameleon_rs.configure(markdown=True) is None
    assert PageTemplate('${markdown: s}')(s='*x*') == '<p><em>x</em></p>\n'


def test_markdown_false_removes_the_profiles() -> None:
    chameleon_rs.configure(markdown=True)
    chameleon_rs.configure(markdown=False)
    with pytest.raises(LookupError):
        PageTemplate('${markdown: x}')(x='a')


def test_other_settings_keep_the_profiles(tmp_path: Any) -> None:
    chameleon_rs.configure(markdown=True)
    chameleon_rs.configure(template_folder=str(tmp_path), auto_reload=True)
    chameleon_rs.configure(partial_search='registered')
    assert list(chameleon_rs._settings.markdown_profiles) == ['markdown']


def test_a_template_class_prefix_wins() -> None:
    chameleon_rs.configure(markdown=True)

    class Shouting(PageTemplate):
        expression_types: ClassVar[dict[str, Any]] = {
            **PageTemplate.expression_types,
            'markdown': PageTemplate.expression_types['structure'],
        }

    assert Shouting('${markdown: s}')(s='*raw*') == '*raw*'
    assert PageTemplate('${markdown: s}')(s='*raw*') == '<p><em>raw</em></p>\n'


def test_compiled_templates_keep_their_profile() -> None:
    chameleon_rs.configure(markdown={'markdown': {'inline': True}})
    page = PageTemplate('${markdown: s}')
    assert page(s='*x*') == '<em>x</em>'
    chameleon_rs.configure(markdown=True)
    assert page(s='*x*') == '<em>x</em>'
    assert PageTemplate('${markdown: s}')(s='*x*') == '<p><em>x</em></p>\n'


def test_clear_forgets_markdown() -> None:
    chameleon_rs.configure(markdown=True, markdown_cache=5)
    chameleon_rs._settings.clear()
    assert chameleon_rs._settings.markdown_profiles == {}
    assert markdown_cache_info() == (1024, 0, 0)


# -- the cache ------------------------------------------------------------------------------


def test_the_cache_returns_the_same_markup() -> None:
    chameleon_rs.configure(markdown={'md': True})
    profile = chameleon_rs._settings.markdown_profiles['md']
    first = profile.convert('*cached*')
    assert profile.convert('*cached*') is first
    assert markdown_cache_info()[:2] == (1024, 1)
    # Equal text from another object is a hit too; different text is not.
    head = '*cac'
    assert profile.convert(f'{head}hed*') is first
    assert profile.convert('*other*') is not first


def test_profiles_dont_share_entries() -> None:
    chameleon_rs.configure(markdown={'a': True, 'b': {'inline': True}})
    a, b = (chameleon_rs._settings.markdown_profiles[k] for k in 'ab')
    assert a.convert('*x*') == '<p><em>x</em></p>\n'
    assert b.convert('*x*') == '<em>x</em>'


def test_the_cache_is_bounded() -> None:
    chameleon_rs.configure(markdown=True, markdown_cache=16)
    profile = chameleon_rs._settings.markdown_profiles['markdown']
    for i in range(200):
        profile.convert(f'item {i}')
        assert markdown_cache_info()[1] <= 16


def test_the_cache_evicts_the_least_recently_used() -> None:
    chameleon_rs.configure(markdown=True, markdown_cache=16)
    profile = chameleon_rs._settings.markdown_profiles['markdown']
    kept = profile.convert('kept')
    first = profile.convert('first')
    for i in range(14):
        profile.convert(f'fill {i}')
    assert profile.convert('kept') is kept  # used again: now the most recent
    for i in range(3):
        profile.convert(f'more {i}')  # over 16: evicts down to 14, oldest first
    assert profile.convert('kept') is kept
    assert profile.convert('first') is not first


def test_the_cache_limits_bytes() -> None:
    from chameleon_rs._native import markdown_cache_limits

    _, max_text, max_bytes = markdown_cache_limits()
    chameleon_rs.configure(markdown=True, markdown_cache=100_000)
    profile = chameleon_rs._settings.markdown_profiles['markdown']
    for i in range(1_200):
        profile.convert(f'{i} ' + 'x' * (max_text - 20))
    _, entries, size = markdown_cache_info()
    assert size <= max_bytes
    assert 0 < entries < 1_200


def test_cache_size_zero_turns_it_off() -> None:
    chameleon_rs.configure(markdown=True, markdown_cache=0)
    profile = chameleon_rs._settings.markdown_profiles['markdown']
    assert profile.convert('*x*') == profile.convert('*x*')
    assert profile.convert('*x*') is not profile.convert('*x*')
    assert markdown_cache_info() == (0, 0, 0)


def test_reconfiguring_empties_the_cache() -> None:
    chameleon_rs.configure(markdown=True)
    chameleon_rs._settings.markdown_profiles['markdown'].convert('*x*')
    assert markdown_cache_info()[1] == 1
    chameleon_rs.configure(markdown=True)
    assert markdown_cache_info()[1] == 0


@pytest.mark.parametrize(
    ('size', 'error'), [(-1, ValueError), (2**64, ValueError), (1.5, TypeError), (True, TypeError), ('big', TypeError)]
)
def test_invalid_cache_sizes(size: Any, error: type[Exception]) -> None:
    with pytest.raises(error, match='markdown_cache must be a number of entries'):
        chameleon_rs.configure(markdown_cache=size)


def test_large_texts_convert_without_the_cache() -> None:
    chameleon_rs.configure(markdown=True)
    text = 'word ' * 20_000
    out = PageTemplate('${markdown: t}')(t=text)
    assert out.startswith('<p>word word') and out.endswith('word</p>\n')
    assert markdown_cache_info()[1] == 0


def test_concurrent_renders() -> None:
    chameleon_rs.configure(markdown=True, markdown_cache=64)
    page = PageTemplate('<div tal:repeat="n items">${markdown: "**%d** " % n + text}</div>')
    long = 'para ' * 2000

    def render(n: int) -> tuple[int, str]:
        return n, page(items=[n % 100, n % 7], text=long if n % 3 == 0 else '*s*')

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(render, range(600)))
    for n, out in results:
        tail = long.strip() if n % 3 == 0 else '<em>s</em>'
        expected = '\n'.join(f'<div><p><strong>{k}</strong> {tail}</p>\n</div>' for k in (n % 100, n % 7))
        assert out == expected


def test_reconfiguring_while_rendering() -> None:
    chameleon_rs.configure(markdown=True, markdown_cache=8)
    page = PageTemplate('${markdown: text}')

    def render(n: int) -> str:
        if n % 50 == 0:
            chameleon_rs.configure(markdown_cache=4 + n % 7)
        return page(text=f'*{n % 13}*')

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(render, range(2_000)))
    assert results == [f'<p><em>{n % 13}</em></p>\n' for n in range(2_000)]
