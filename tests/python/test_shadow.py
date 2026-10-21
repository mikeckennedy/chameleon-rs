"""Shadow mode (`chameleon_rs.shadow`): stock Chameleon serves (or chameleon-rs does) and each
render is repeated with the other engine, differences reported."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import chameleon
import chameleon.zpt.template
import chameleon_rs.template
import pytest
from chameleon_rs import shadow

# The engines agree on every template the suites know, so the tests make chameleon-rs's output
# differ: a template containing MARK renders with a comment appended.
DIFFERS = '<p>MARK ${1 + 1}</p>'


def identity(msgid: Any, **kw: Any) -> Any:
    return msgid


@pytest.fixture(autouse=True)
def marked(monkeypatch: pytest.MonkeyPatch) -> None:
    original = chameleon_rs.template.PageTemplate.render

    def render(self: Any, *args: Any, **kw: Any) -> Any:
        out = original(self, *args, **kw)
        return out + '<!--rs-->' if 'MARK' in out else out

    monkeypatch.setattr(chameleon_rs.template.PageTemplate, 'render', render)


@pytest.fixture
def reports() -> Iterator[list[shadow.ShadowDifference]]:
    found: list[shadow.ShadowDifference] = []
    yield found
    shadow.uninstall()


def test_off_by_default_and_uninstall_restores() -> None:
    assert not shadow.installed()
    render = chameleon.zpt.template.PageTemplate.__dict__['render']
    shadow.install(report=lambda d: None)
    assert chameleon.zpt.template.PageTemplate.__dict__['render'] is not render
    shadow.uninstall()
    assert chameleon.zpt.template.PageTemplate.__dict__['render'] is render
    assert not shadow.installed()


def test_identical_renders_report_nothing(reports: list[Any]) -> None:
    shadow.install(report=reports.append)
    t = chameleon.PageTemplate('<p tal:repeat="i items" class="${i}">${i * 2}</p>')
    assert t(items=[1, 2]) == '<p class="1">2</p>\n<p class="2">4</p>'
    assert reports == []


@pytest.mark.parametrize('serve', ['chameleon', 'rust'])
def test_a_difference_is_reported_and_the_served_engine_wins(reports: list[Any], serve: str) -> None:
    shadow.install(report=reports.append, serve=serve)
    out = chameleon.PageTemplate(DIFFERS)()
    expected = {'chameleon': '<p>MARK 2</p>', 'rust': '<p>MARK 2</p><!--rs-->'}
    assert out == expected[serve]
    [difference] = reports
    assert difference.served == serve
    assert difference.chameleon == expected['chameleon']
    assert difference.rust == expected['rust']
    assert difference.diff.startswith('--- chameleon')


def test_exceptions_are_compared_and_the_served_one_raised(reports: list[Any]) -> None:
    shadow.install(report=reports.append)
    t = chameleon.PageTemplate('<p>${1 / x}</p>')
    with pytest.raises(ZeroDivisionError):
        t(x=0)
    assert reports == []  # same type and message (object addresses aside)
    assert t(x=1) == '<p>1.0</p>'


def test_partials_and_template_arguments_render_with_twins(reports: list[Any], tmp_path: Path) -> None:
    (tmp_path / 'partial.pt').write_text(DIFFERS)
    (tmp_path / 'page.pt').write_text('<main>${structure: render_partial("partial.pt")}</main>')
    (tmp_path / 'layout.pt').write_text('<div metal:define-macro="main">L<i metal:define-slot="s"/></div>')
    (tmp_path / 'uses.pt').write_text(
        '<x metal:use-macro="layout.macros[\'main\']"><b metal:fill-slot="s">${a}</b></x>'
    )
    loader = chameleon.PageTemplateLoader(str(tmp_path), translate=identity)

    def render_partial(name: str, **kw: Any) -> str:
        return loader[name].render(**kw)

    shadow.install(report=reports.append)
    # The partial differs: only its chameleon-rs twin rendering it shows that.
    out = loader['page.pt'](render_partial=render_partial)
    assert out == '<main><p>MARK 2</p></main>'
    assert [Path(d.filename).name for d in reports] == ['page.pt']
    # A stock template passed as an argument: its twin's macros are used, without difference.
    assert loader['uses.pt'](layout=loader['layout.pt'], a=1) == '<div>L<b>1</b></div>'
    assert len(reports) == 1


def test_sample_rate_zero_renders_once(reports: list[Any]) -> None:
    shadow.install(report=reports.append, sample_rate=0.0)
    assert chameleon.PageTemplate(DIFFERS)() == '<p>MARK 2</p>'
    assert reports == []


def test_templates_cooked_before_install_are_served_unshadowed(reports: list[Any]) -> None:
    t = chameleon.PageTemplate(DIFFERS)
    shadow.install(report=reports.append)
    t()
    assert reports == []


def test_settings_are_validated() -> None:
    with pytest.raises(ValueError, match='serve'):
        shadow.install(serve='both')
    with pytest.raises(ValueError, match='sample_rate'):
        shadow.install(sample_rate=2)
    assert not shadow.installed()


class Panic(BaseException):
    """Like `pyo3_runtime.PanicException`: not an `Exception`."""


class Unprintable(Exception):
    def __str__(self) -> str:
        raise ValueError('no str')


@pytest.mark.parametrize('error', [Panic('boom'), Unprintable()])
def test_a_failing_twin_never_reaches_the_request(
    reports: list[Any], monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def fail(self: Any, *args: Any, **kw: Any) -> Any:
        raise error

    monkeypatch.setattr(chameleon_rs.template.PageTemplate, 'render', fail)
    shadow.install(report=reports.append)
    assert chameleon.PageTemplate('<p>${1}</p>')() == '<p>1</p>'
    [difference] = reports
    assert difference.rust.startswith(type(error).__name__)


def test_a_failing_report_never_reaches_the_request(caplog: pytest.LogCaptureFixture) -> None:
    def report(difference: Any) -> None:
        raise Panic('report')

    shadow.install(report=report)
    try:
        assert chameleon.PageTemplate(DIFFERS)() == '<p>MARK 2</p>'
    finally:
        shadow.uninstall()
    assert 'reporting a difference failed' in caplog.text


def test_default_log_is_capped_and_deduplicated(caplog: pytest.LogCaptureFixture) -> None:
    shadow.install(log_interval=3600)
    try:
        t = chameleon.PageTemplate('<p>MARK ' + 'x' * 1000 + '</p>' + '<i>y</i>' * 2000)
        for _ in range(3):
            t()
    finally:
        shadow.uninstall()
    [record] = [r for r in caplog.records if r.name == 'chameleon_rs.shadow']
    message = record.getMessage()
    assert 'sha256' in message
    assert max(len(line) for line in message.splitlines()) < 260
    assert len(message) < 4096 + 400
