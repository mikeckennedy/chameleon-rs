# Security

Templates are code: like Python source, they run with your application's rights, and an expression in one can do anything Python can. The data you render them with is what may come from users. This page says what chameleon-rs does with that data, where its protection stops, and what to do there. Everything here behaves exactly as in Chameleon unless it says otherwise.


# What's escaped

- **Text**: `${x}`, `tal:content="x"` and `tal:replace="x"` escape `&`, `<` and `>`.
- **Attribute values**: `title="${x}"` and `tal:attributes="title x"` also escape the attribute's own quote character (`"` as `"`, `'` as `'`).
- **`markdown:`**: in element content, raw HTML in the text is escaped and `javascript:`, `vbscript:`, `file:` and `data:` links are removed, unless a profile sets `raw_html='allow'`. In an attribute value the result is escaped like any string. For text from users, give a `heading_ids` profile a prefix, so headings can't take ids your page's scripts rely on. See [Markdown](markdown.md).

Nothing is escaped for `structure`, `structure:`, or an object with an `__html__` method (such as `Markup`). Use them only for markup you made or have already sanitized.


# Where escaping isn't enough

These are the HTML contexts where escaping `&`, `<`, `>` and the quote doesn't make data safe.

- **Attribute names from data.** `tal:attributes="attrs"` with a dictionary escapes the values but writes the keys as they are. A key such as `onclick="x" a` adds an attribute. Build attribute dictionaries from fixed names only.

- **Unquoted attributes.** In `<p title=${x}>`, a space in the value starts a new attribute (`a onmouseover=alert(1)`). Always quote attribute values.

- **CDATA sections.** `<![CDATA[${x}]]>` is written unescaped. Keep data out of CDATA.

- **Inside `<script>` and `<style>`.** `${x}` there is HTML-escaped, which stops a `</script>` but not a quote: `var name = "${x}";` can be broken out of. Put the data in an attribute instead and read it from JavaScript:

  ``` html
  <div id="app" data-user="${user_json}"></div>
  ```

  where `user_json` is `json.dumps(...)` of what the script needs.

- **URLs.** `href="${url}"` is escaped, but a `javascript:` URL is still a script. Check the scheme of any URL that comes from users before you render it.

- **Translations.** The text a translation function returns is written as markup, as in Chameleon. Treat message catalogs like templates: only from sources you trust.


# Template names from requests

A name that comes from a request must never reach a lookup that accepts any path.

- **Safe with request data**: [render_partial](../reference/render_partial.md#chameleon_rs.render_partial), [chameleon_rs.starlette.Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates), [chameleon_rs.litestar.ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) and [chameleon_rs.robyn.ChameleonTemplate](../reference/robyn.ChameleonTemplate.md#chameleon_rs.robyn.ChameleonTemplate). They refuse names that leave their folder: absolute paths, `..` out of it, `package:path` specs, backslashes on the Jinja-style APIs. A refused name is "not found", like a missing file. The check is on the name, so a symlink that you put inside the folder is still followed, wherever it points (Jinja's `FileSystemLoader` does the same).
- **Names from code only**: the decorator APIs (`@template`, [render](../reference/flask.render.md#chameleon_rs.flask.render), [response](../reference/flask.response.md#chameleon_rs.flask.response) in each integration), [PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) and `load:` expressions. They accept `../` and absolute paths, as the original libraries and Chameleon do.

If a page is chosen by the request, map the request value to a name you wrote:

``` python
PAGES = {'about': 'pages/about.pt', 'team': 'pages/team.pt'}
template = PAGES.get(slug)  # None for anything else: a 404
```


# Render data

Keyword arguments to [render()](../reference/flask.render.md#chameleon_rs.flask.render) become names in the template, and they win over most built-in names: `len`, `str` and other Python builtins in expressions, and `translate`, `target_language` and `nothing`. (In `<?python ?>` code blocks, builtins can't be replaced.) So never pass a request's parameters straight in, as in `render(**request.args)`. Build the data in your view, with the names the template expects.


# Errors and logs

When a render fails, the error message lists the template's variables and their values, as in Chameleon: strings appear as they are. That text goes to your logs. None of the framework integrations put it in an HTTP response: you get the framework's own error page, unless its debug mode is on. Keep debug mode off in production, and don't render with secrets that the template doesn't need.

[Shadow mode](switching-from-chameleon.md#check-your-own-app-first-shadow-mode) logs up to 4 KB of the difference between the two engines' output, which can include users' data. It's meant for staging, and it's off unless you install it.


# Untrusted input size

chameleon-rs keeps the cost of a render in proportion to its input:

- Wide Markdown tables convert in linear time.
- The Markdown cache has a limit in entries and in bytes.
- A template nested too deeply to compile raises `RecursionError` instead of crashing the process.

A large input still costs time in proportion to its size, so limit the size of what users can submit (a comment's length, say) in your application, as you would anyway.
