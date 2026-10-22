# Getting started

This page installs chameleon-rs, renders a template from a string, then sets up a folder of template files the way a web app would.


# Install

``` bash
uv pip install chameleon-rs
```

chameleon-rs supports CPython 3.12 to 3.15, including the free-threaded builds (3.14t, 3.15t). The wheel contains the compiled engine and has no dependencies. The framework integrations use the framework your app already has, so there are no extras to install.


# Render a template string

[PageTemplate](../reference/PageTemplate.md#chameleon_rs.PageTemplate) compiles a template from a string. Calling it renders the template, with the keyword arguments as the template's variables:


``` python
from chameleon_rs import PageTemplate

page = PageTemplate("""

  <h1>Hello, ${name}!</h1>
  <ul tal:condition="items">
    <li tal:repeat="item items">${repeat.item.number}. ${item}</li>
  </ul>

""")

print(page(name='World', items=['templates', 'in', 'Rust']))
```


    <div class="greeting">
      <h1>Hello, World!</h1>
      <ul>
        <li>1. templates</li>
        <li>2. in</li>
        <li>3. Rust</li>
      </ul>
    </div>


The template compiles once, when it's first rendered. Every later render reuses the compiled program, so keep template objects around (module level, or in a loader) rather than building one per request.

`${...}` inserts a value with HTML escaping. To insert markup as-is, use `structure:`, or pass an object with an `__html__` method:


``` python
page = PageTemplate('<p>${text}</p> <p>${structure: text}</p>')
print(page(text='<b>bold</b>'))
```


    <p><b>bold</b></p> <p><b>bold</b></p>


# Render templates from files

[PageTemplateFile](../reference/PageTemplateFile.md#chameleon_rs.PageTemplateFile) loads one file. [PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) loads templates by name from one or more folders, and caches each compiled template:

Suppose your templates live in a folder like this:

``` text
templates/
├── home/
│   └── index.pt
└── shared/
    └── video.pt
```

with `home/index.pt`:

``` html
<html>
  <body>
    <h1>${title}</h1>
    <div tal:repeat="video videos">
      ${render_partial('shared/video.pt', video=video)}
    </div>
  </body>
</html>
```

and `shared/video.pt`:

``` html
[${video.title}](/videos/${video.id})
```

Configure the folder once at startup with [chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure), then load templates by name:


``` python
from dataclasses import dataclass

import chameleon_rs
from chameleon_rs import PageTemplateLoader


@dataclass
class Video:
    id: int
    title: str


chameleon_rs.configure(template_folder=template_folder)
templates = PageTemplateLoader([template_folder])

page = templates['home/index.pt']
print(page(title='Latest videos', videos=[Video(1, 'Intro'), Video(2, 'Partials')]))
```


    <html>
      <body>
        <h1>Latest videos</h1>
        <div>
          [Intro](/videos/1)
        </div>
        <div>
          [Partials](/videos/2)
        </div>
      </body>
    </html>


[render_partial](../reference/render_partial.md#chameleon_rs.render_partial) is available in every template, with nothing to register or pass in. [configure()](../reference/configure.md#chameleon_rs.configure) tells it where to find partials. See [Partials](partials.md).

In a real app, build the folder path relative to your code, so it doesn't depend on the working directory:

``` python
from pathlib import Path

import chameleon_rs

template_folder = (Path(__file__).parent / 'templates').as_posix()
chameleon_rs.configure(template_folder=template_folder, auto_reload=dev_mode)
```

`auto_reload=True` recompiles a template when its file changes. Turn it on in development and leave it off in production.


# In a web app

In a web app you rarely call templates yourself. The framework integrations configure the folder and render what your views return:

``` python
import fastapi

import chameleon_rs.fastapi as fastapi_chameleon

app = fastapi.FastAPI()
fastapi_chameleon.configure(template_folder=template_folder, auto_reload=dev_mode, app=app)


@app.get('/')
@fastapi_chameleon.template('home/index.pt')
async def index():
    return {'title': 'Latest videos', 'videos': await load_videos()}
```

[Web frameworks](frameworks.md) shows the same view in Flask, Quart, FastAPI, Starlette, Robyn, Litestar and Pyramid, and links to each framework's guide.


# Next steps

- Already have a Chameleon app? See [Switching from Chameleon](switching-from-chameleon.md).
- Pick your framework: [Flask and Quart](flask.md), [FastAPI](fastapi.md), [Starlette](starlette.md), [Robyn](robyn.md), [Litestar](litestar.md) or [Pyramid](pyramid.md).
- Async views: [Async rendering](async.md) explains how a render awaits data without blocking the event loop.
- Content in Markdown? The [`markdown:` expression](markdown.md) converts it while the page renders.
- The [language reference](../language/index.md) covers TAL, METAL, i18n and expressions.
