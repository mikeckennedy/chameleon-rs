# Template language

chameleon-rs implements Chameleon's template language unchanged: the same TAL attributes, METAL macros, i18n attributes, `${...}` interpolation and expression types, with the same output. Templates you wrote for Chameleon work as they are.

This page is a quick tour. The [language reference](../language/index.md) covers every statement in detail.


# A tour in one template


``` python
from chameleon_rs import PageTemplate

page = PageTemplate("""
<section tal:define="count len(items)">
  <h2>${title} <small tal:condition="count">(${count})</small></h2>
  <p tal:condition="not count">Nothing here yet.</p>
  <ul>
    <li tal:repeat="item items"
        class="${'odd' if repeat.item.odd else 'even'}"
        tal:content="item.upper()">placeholder</li>
  </ul>
  [More](${url})
</section>
""")

print(page(title='Tags', items=['rust', 'python', 'html'], url='/tags'))
```


    <section>
      <h2>Tags <small>(3)</small></h2>
      
      <ul>
        <li
            class="even">RUST</li>
        <li
            class="odd">PYTHON</li>
        <li
            class="even">HTML</li>
      </ul>
      [More](/tags)
    </section>


# The pieces

| Syntax | What it does |
|----|----|
| `${expr}` | Insert a value, HTML-escaped. Works in text and in attribute values. |
| `${structure: expr}` | Insert markup without escaping. Objects with `__html__` are also inserted as-is. |
| `tal:define="name expr"` | Bind a variable for the element and its children (`global` for the rest of the template). |
| `tal:condition="expr"` | Keep the element only if the expression is true. |
| `tal:repeat="item items"` | Repeat the element for each item. `repeat.item` gives `index`, `number`, `odd`, `even`, `start`, `end`, `length`, … |
| `tal:content="expr"` / `tal:replace="expr"` | Replace the element's content, or the whole element. |
| `tal:attributes="name expr; ..."` | Set or remove attributes. |
| `tal:omit-tag=""` | Drop the element's tags and keep its content. |
| `tal:switch` / `tal:case` | Choose one of several branches. |
| `tal:on-error="expr"` | Render a fallback if the element raises. |
| `metal:define-macro` / `metal:use-macro` / `metal:fill-slot` | Layouts and reusable fragments. See below. |
| `i18n:translate`, `i18n:attributes`, `i18n:name`, `i18n:domain` | Mark text for translation. |

Expressions are Python by default. Other expression types are written with a prefix: `string:`, `exists:`, `not:`, `import:`, `load:` and `structure:`.


# Layouts with METAL

A layout defines a macro with slots, and each page fills them:


``` python
layout = PageTemplate("""
<html metal:define-macro="layout">
  <head><title metal:define-slot="title">Site</title></head>
  <body><main metal:define-slot="content"></main></body>
</html>
""")

page = PageTemplate("""
<html metal:use-macro="layout.macros['layout']">
  <title metal:fill-slot="title">Home</title>
  <main metal:fill-slot="content"><p>Welcome, ${user}.</p></main>
</html>
""")

print(page(layout=layout, user='Ada'))
```


    <html>
      <head><title>Home</title></head>
      <body><main><p>Welcome, Ada.</p></main></body>
    </html>


In a template loaded from a file, `load:` loads the layout relative to the page: `metal:use-macro="load: ../shared/_layout.pt"`.


# What chameleon-rs adds

Two additions, both of which leave existing templates unchanged:

- **[render_partial](../reference/render_partial.md#chameleon_rs.render_partial)**, a builtin in every template: `${render_partial('shared/card.pt', item=item)}`. See [Partials](partials.md).
- **`markdown:`**, an expression type you turn on with `chameleon_rs.configure(markdown=True)`: `${markdown: post.body}`. See [Markdown](markdown.md).

And one way to render: `await template.render_async(**model)` awaits awaitable values where the template uses them. The language is the same; see [Async rendering](async.md).
