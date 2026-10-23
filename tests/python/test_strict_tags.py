"""`strict_tags` (chameleon-rs, opt-in): an unclosed element carrying TAL, METAL or i18n
attributes is a `ParseError` at its start tag. Chameleon compiles it to an empty element, so
the attributes miss the content after it and the mistake surfaces elsewhere, or not at all.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import chameleon
import chameleon_rs
import chameleon_rs.config
import pytest
from chameleon_rs.exc import ParseError

HIDDEN = '<section>\n  <div tal:condition="False">\n    <p>secret</p>\n</section>'
REPEAT = '<main>\n  <ul tal:repeat="i items">\n    <li>${i}</li>\n</main>'


def test_off_by_default_matching_chameleon() -> None:
    assert chameleon_rs.PageTemplate.strict_tags is False
    want = chameleon.PageTemplate(HIDDEN)()
    assert chameleon_rs.PageTemplate(HIDDEN)() == want
    assert 'secret' in want


def test_unclosed_condition_points_at_its_start_tag() -> None:
    with pytest.raises(ParseError) as info:
        chameleon_rs.PageTemplate(HIDDEN, strict_tags=True)()
    assert str(info.value) == (
        'Unclosed element <div>: its tal:condition applies to the start tag only, not to the '
        'content after it. Add the missing </div>, or end the tag with "/>" if it is meant to be '
        'empty.\n'
        '\n'
        ' - String:     "div"\n'
        ' - Filename:   <string>\n'
        ' - Location:   (line 2: col 3)\n'
        ' - Source:     <div tal:condition="False">\n'
        '                ^^^'
    )


def test_the_error_names_the_cause_not_the_symptom() -> None:
    # Chameleon reports a NameError for `i` on line 3; the missing </ul> is on line 2.
    with pytest.raises(NameError):
        chameleon_rs.PageTemplate(REPEAT)(items=[1])
    with pytest.raises(ParseError, match=r'(?s)Unclosed element <ul>: its tal:repeat.*line 2: col 3'):
        chameleon_rs.PageTemplate(REPEAT, strict_tags=True)(items=[1])


@pytest.mark.parametrize(
    'source',
    [
        '<div tal:condition="True">x</div>',
        '<div tal:condition="True" />',
        '<form><input tal:attributes="value 1"><img tal:attributes="src 2"><br></form>',
        '<div><p>a<p>b<li>c</div>',
        '<div>${1}<p class="${2}">x</div>',
    ],
)
def test_allowed_markup_renders_as_without_the_setting(source: str) -> None:
    assert chameleon_rs.PageTemplate(source, strict_tags=True)() == chameleon.PageTemplate(source)()


def test_data_attributes_count() -> None:
    source = '<s><div data-tal-condition="False"><p>x</p></s>'
    with pytest.raises(ParseError, match=r'<div>: its tal:condition'):
        chameleon_rs.PageTemplate(source, strict_tags=True, enable_data_attributes=True)()


def test_loader_keyword(tmp_path: Path) -> None:
    (tmp_path / 'page.pt').write_text(REPEAT)
    loader = chameleon_rs.PageTemplateLoader(str(tmp_path), strict_tags=True)
    with pytest.raises(ParseError, match=r'Unclosed element <ul>'):
        loader['page.pt'](items=[1])


def test_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('CHAMELEON_STRICT_TAGS', 'true')
    try:
        importlib.reload(chameleon_rs.config)
        assert chameleon_rs.config.STRICT_TAGS is True
    finally:
        monkeypatch.delenv('CHAMELEON_STRICT_TAGS')
        importlib.reload(chameleon_rs.config)
    assert chameleon_rs.config.STRICT_TAGS is False


def test_the_setting_is_part_of_the_cache_digest() -> None:
    body = '<p>x</p>'
    plain = chameleon_rs.PageTemplate(body)
    strict = chameleon_rs.PageTemplate(body, strict_tags=True)
    assert plain.digest(body, []) != strict.digest(body, [])
    # Off, the digest is what it was before the setting existed.
    assert chameleon_rs.PageTemplate(body, strict_tags=False).digest(body, []) == plain.digest(body, [])


def test_configure_reaches_partials_and_integrations(tmp_path: Path) -> None:
    import chameleon_rs._settings
    import chameleon_rs.fastapi

    (tmp_path / 'card.pt').write_text(REPEAT)
    (tmp_path / 'page.pt').write_text("<div>${render_partial('card.pt', items=[1])}</div>")
    try:
        settings = chameleon_rs.configure(template_folder=str(tmp_path), strict_tags=True)
        assert settings is not None
        assert settings.strict_tags is True
        assert repr(settings).endswith(', strict_tags=True)')
        with pytest.raises(ParseError, match=r'Unclosed element <ul>'):
            settings.loader.load('page.pt')()
        # A framework's own configure() starts from it.
        chameleon_rs.fastapi.configure(template_folder=str(tmp_path))
        integration = chameleon_rs._settings.views['fastapi']
        assert integration is not None
        assert integration.strict_tags is True
        # Turned off again.
        off = chameleon_rs.configure(strict_tags=False)
        assert off is not None
        assert off.strict_tags is False
    finally:
        chameleon_rs._settings.clear()
