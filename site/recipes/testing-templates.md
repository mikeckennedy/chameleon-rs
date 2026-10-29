# Testing templates with pytest


# Every template compiles

A typo in a TAL attribute or an expression is a compile error. Chameleon compiles a template when it's first rendered, so a broken template can go unnoticed until someone opens that page. This test compiles all of them:

``` python
# tests/test_templates_compile.py
from pathlib import Path

import pytest

from chameleon_rs import PageTemplateFile

TEMPLATES = Path(__file__).parent.parent / 'myapp' / 'templates'


@pytest.mark.parametrize('path', sorted(TEMPLATES.rglob('*.pt')), ids=str)
def test_template_compiles(path):
    PageTemplateFile(path).cook_check()
```

`cook_check()` compiles without rendering, so the test needs no model. chameleon-rs compiles in about 0.05 ms per template, so even a few hundred templates take well under a second.

The `CHAMELEON_EAGER=1` environment variable does the same at load time: file templates then compile as soon as they're created.


# Test the rendered output

Render a partial with the model your view would pass, and assert on the HTML:

``` python
import chameleon_rs


@pytest.fixture(autouse=True)
def templates():
    chameleon_rs.configure(template_folder=TEMPLATES)
    yield
    chameleon_rs.partials.clear()


def test_video_card_shows_title():
    html = chameleon_rs.render_partial('shared/video_card.pt', video=Video(1, 'Intro')).html_text
    assert '[Intro](/videos/1)' in html
```

The fixture's [clear()](../reference/flask.clear.md#chameleon_rs.flask.clear) forgets the configuration, so tests that configure a different folder don't leak into each other.

For a Flask app, use its test client. See [HTMX fragments](htmx-fragments.md) for an example.


# Compare against Chameleon

While you're migrating, a test can render the same template with both engines and compare. Install stock Chameleon in the test environment only:

``` python
import chameleon
import chameleon_rs


def test_same_output_as_chameleon():
    path = str(TEMPLATES / 'home' / 'index.pt')
    model = {'title': 'Home', 'videos': []}
    assert chameleon_rs.PageTemplateFile(path)(**model) == chameleon.PageTemplateFile(path)(**model)
```

On a running staging app, [shadow mode](../user-guide/switching-from-chameleon.md#check-your-own-app-first-shadow-mode) does this for every render.
