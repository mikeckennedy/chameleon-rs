# Markdown

chameleon-rs can convert Markdown to HTML while it renders, with a `markdown:` expression:

``` html
<article>${markdown: episode.notes}</article>
<div tal:content="markdown: comment.text"></div>
```

It's opt-in: Chameleon has nothing like it, so you turn it on with [configure()](../reference/configure.md#chameleon_rs.configure) and use it in new templates. The converter is built into chameleon-rs (comrak, in Rust), so there's no Markdown package to install.


# Turn it on


``` python
import chameleon_rs
from chameleon_rs import PageTemplate

chameleon_rs.configure(markdown=True)

page = PageTemplate('<article>${markdown: notes}</article>')
print(page(notes='# Show notes\n\nWe talked about **templates** and `Rust`.'))
```


    <article><h1>Show notes</h1>
    <p>We talked about <strong>templates</strong> and <code>Rust</code>.</p>
    </article>


`configure(markdown=True)` registers the `markdown:` prefix with the defaults. Call it once at startup, before your templates compile: a template takes the Markdown settings in effect when it's compiled.


# What it supports

[CommonMark](https://commonmark.org/) plus the GitHub extensions: tables, strikethrough, task lists, autolinks for bare URLs, and footnotes.


``` python
notes = """\
| Engine | Speed |
|---|---|
| chameleon-rs | ~~slow~~ fast |

- [x] Ship it, see https://example.com
"""
print(page(notes=notes))
```


    <article><table>
    <thead>
    <tr>
    <th>Engine</th>
    <th>Speed</th>
    </tr>
    </thead>
    <tbody>
    <tr>
    <td>chameleon-rs</td>
    <td><del>slow</del> fast</td>
    </tr>
    </tbody>
    </table>
    <ul>
    <li><input type="checkbox" checked="" disabled="" /> Ship it, see [https://example.com](https://example.com)</li>
    </ul>
    </article>


Fenced code blocks get a `language-…` class, so a client-side highlighter such as highlight.js or Prism can color them:


``` python
print(page(notes='```python\nprint("hi")\n```'))
```


    <article><pre><code class="language-python">print("hi")
    </code></pre>
    </article>


# Values

- The text after `markdown:` is an ordinary expression, so a `|` fallback works: `markdown: episode.notes | 'No notes yet.'`. As everywhere in TALES, the fallback is used when the expression raises (a missing attribute, say), not when its value is `None`.
- `None` and `''` render nothing. Any other non-string value is converted with `str()` first.
- `default` works as for any expression: `tal:content="markdown: page.notes | default"` keeps the element's own content when `page.notes` is missing.
- The result is markup, like `structure:`, so it isn't escaped again in element content.
- In an attribute value (`alt="${markdown: x}"`, `tal:attributes`), the result is escaped like any string. Markdown's HTML has quoted attributes of its own (`<a href="...">`), which would otherwise close the surrounding attribute and let the text add new ones, such as an event handler. Use Markdown in element content; `${structure: markdown: x}` writes it raw anyway.


``` python
print(PageTemplate('<div>${markdown: missing.notes | "*Coming soon*"}</div>')())
print(PageTemplate('<p>[${markdown: notes}]</p>')(notes=None))
```


    <div><p><em>Coming soon</em></p>
    </div>
    <p>[]</p>


# Safe by default

Raw HTML in the Markdown is shown as text, not passed through. That's the right default for text you don't fully control, such as user comments:


``` python
print(page(notes='Nice post! <script>alert("hi")</script>'))
```


    <article><p>Nice post! <script>alert("hi")</script></p>
    </article>


Links with a `javascript:`, `vbscript:`, `file:` or `data:` URL (other than an image) lose the URL too.

For content you trust, such as your own articles, allow raw HTML in a separate profile (see below). `raw_html: 'allow'` lets those URLs through as well.

"Safe" here is about markup and requests, not CPU time. Some inputs are slow to convert: a table with thousands of columns can take seconds. If anyone can post the text, limit its length when you accept it, and consider turning tables off for that profile (`'extensions': {'table': False}`).


# Profiles: one prefix per kind of content

Different content needs different settings: your own articles may allow HTML, while comments shouldn't even load images. Each profile is its own expression prefix:


``` python
chameleon_rs.configure(markdown={
    'markdown': {},                                     # the defaults
    'md-trusted': {'raw_html': 'allow', 'heading_ids': True},
    'md-comment': {'images': False},
    'md-inline': {'inline': True},
})

page = PageTemplate("""\
<h1>${md-inline: post.title}</h1>
<article>${md-trusted: post.body}</article>
<div tal:repeat="c post.comments">${md-comment: c}</div>
""")


class Post:
    title = 'Hello *world*'
    body = '## Intro\n\nSome <abbr title="HyperText Markup Language">HTML</abbr>.'
    comments = ['Look: ![tracker](https://example.com/pixel.png)']


print(page(post=Post()))
```


    <h1>Hello <em>world</em></h1>
    <article><h2 id="intro">Intro<a href="#intro" aria-label="Link to heading 'Intro'" data-heading-content="Intro" class="anchor"></a></h2>
    <p>Some <abbr title="HyperText Markup Language">HTML</abbr>.</p>
    </article>
    <div><p>Look: tracker</p>
    </div>


A profile name is a prefix, so it uses lowercase letters, digits, `-` and `_`, and can't be one of Chameleon's own (`python`, `string`, `structure`, …). A template class that defines a prefix itself in `expression_types` keeps its own.


## Profile options

| Option | Default | Meaning |
|----|----|----|
| `raw_html` | `'escape'` | `'escape'` shows raw HTML as text. `'allow'` passes it through: trusted content only. |
| `images` | `True` | `False` renders an image as its alt text, so the text can't make the page load a remote image. |
| `inline` | `False` | Drops the `<p>` around output that's a single paragraph, for titles and one-liners in element content (`<h1>${md-inline: title}</h1>`). |
| `heading_ids` | `False` | `True` gives headings GitHub-style ids and anchor links. A string adds a prefix to each id, such as `'doc-'` (letters, digits, `-` and `_`). |
| `classes` | `{}` | A class attribute per element, such as `{'table': 'table is-striped', 'pre': 'code'}`. Elements: `p`, `h1`-`h6`, `blockquote`, `ul`, `ol`, `li`, `pre`, `code`, `table`, `a`, `img`, `hr`. See the notes below. |
| `extensions` | all on | Switch GFM extensions off: `{'table': False}`. Keys: `table`, `strikethrough`, `tasklist`, `autolink`, `footnotes`. |


``` python
chameleon_rs.configure(markdown={'markdown': {'classes': {'table': 'table is-striped', 'blockquote': 'note'}}})
print(PageTemplate('${markdown: s}')(s='> Tip\n\n| a |\n|---|\n| 1 |'))
```


    <blockquote class="note">
    <p>Tip</p>
    </blockquote>
    <table class="table is-striped">
    <thead>
    <tr>
    <th>a</th>
    </tr>
    </thead>
    <tbody>
    <tr>
    <td>1</td>
    </tr>
    </tbody>
    </table>


Notes on `classes`:

- Only elements the Markdown produces get a class. Raw HTML in the text never does.
- `code` is inline code. A fenced block's `<code>` keeps its `language-…` class; style it through `pre`.
- The footnote list, footnote references and back-links, and heading anchor links don't get the `ol`, `li` and `a` classes. Footnote paragraphs do get the `p` class.
- A paragraph inside a tight list (no blank lines between items) has no `<p>` tag, so it gets no `p` class.

Each conversion numbers its heading ids on its own, so two `markdown:` blocks with `heading_ids` on one page can repeat an id. Give each block a profile with its own prefix.

For text from users, always give `heading_ids` a prefix (`'user-content-'`, as GitHub does). Without one, a heading such as `# csrf_token` gets `id="csrf_token"`, and an element id can shadow a JavaScript global of the same name or answer `getElementById` before your own element does.

A bad setting raises `ValueError` (or `TypeError` for a value of the wrong type) when you call [configure()](../reference/configure.md#chameleon_rs.configure), and changes nothing.


# Speed and the cache

The converter is fast: a 5 KB document with tables, lists, code and footnotes converts in about 55 µs, about 26x faster than mistune with the same extensions.

On top of that, chameleon-rs keeps recent conversions in a cache shared by all threads: the same text with the same profile isn't converted twice. A cache hit costs about 0.3 µs for a 5 KB document. The cache holds 1,024 conversions by default, and at most 64 MB of text and HTML whatever the count. Texts over 64 KB are converted every time rather than cached:

``` python
chameleon_rs.configure(markdown_cache=4096)   # entries; 0 turns the cache off
```

Changing the Markdown settings empties the cache.


# Async renders

[render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) works with `markdown:`. If the value is awaitable (`${markdown: load_body()}` with an `async def load_body()`), it is awaited and then converted, and a `@concurrent` helper still runs alongside the page's other concurrent calls. As for every expression type, only the expression's value is awaited, not values inside it: `${markdown: body().text}` reads `.text` of the coroutine. A synchronous [render](../reference/flask.render.md#chameleon_rs.flask.render) converts what it gets, so load the text in your view there.
