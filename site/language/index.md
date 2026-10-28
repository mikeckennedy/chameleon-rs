# Language reference

> **Note**
>
> Adapted from [Chameleon's language reference](https://chameleon.readthedocs.io/) ([source](https://github.com/malthe/chameleon)), modified for chameleon-rs. Used under Chameleon's license; see `LICENSE.txt` and `COPYRIGHT.txt` in the repository.

chameleon-rs uses Chameleon's template language unchanged. This reference can be read as a general introduction to the *page templates* language. It's split into parts that correspond to the main language features:

| Page | What it covers |
|----|----|
| [Basics (TAL)](tal.md) | The statements that define variables, test conditions, repeat, replace and omit elements, and set attributes. |
| [Expressions (TALES)](expressions.md) | The expression types (`python`, `string`, `exists`, `not`, `import`, [load](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader.load), `structure`) and the built-in names. |
| [Macros (METAL)](macros.md) | Defining, using and extending macros, and filling their slots. |
| [Translation (I18N)](i18n.md) | The `i18n` namespace, translation functions and message extraction. |
| [Inline syntax](syntax.md) | The `${...}` operator, `<?python ?>` code blocks and markup comments. |
| [Extensions and differences](extensions.md) | What Chameleon adds to the page templates language, and how it differs from Zope's. |


# Syntax

You can safely [skip this section](tal.md) if you're familiar with how template languages work or just want to learn by example.

An *attribute language* is a programming language designed to render documents written in XML or HTML markup. The input must be a well-formed document. The output from the template is usually XML-like but isn't required to be well-formed.

The statements of the language are document tags with special attributes, and look like this:

``` html
<p namespace-prefix:command="argument"> ... </p>
```

In the above example, the attribute `namespace-prefix:command="argument"` is the statement, and the entire paragraph tag is the statement's element. The statement's element is the portion of the document on which this statement operates.

The namespace prefixes are typically declared once, at the top of a template (note that prefix declarations for the template language namespaces are omitted from the template output):

``` html
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:tal="http://xml.zope.org/namespaces/tal"
      xmlns:metal="http://xml.zope.org/namespaces/metal"
      xmlns:i18n="http://xml.zope.org/namespaces/i18n">
  ...
</html>
```

Thankfully, sane namespace prefix defaults are in place to let us skip most of the boilerplate:

``` html
<html xmlns="http://www.w3.org/1999/xhtml">
  <body>
    <p tal:content="text"> ... </p>
  </body>
</html>
```

Note how `tal` is used without an explicit namespace declaration. Chameleon sets up defaults for `metal` and `i18n` as well.

> **Note: Note**
>
> Default prefixes are a special feature of Chameleon.

If the `enable_data_attributes` option is set then you can use `data-prefix-command="argument"` in addition to the namespace prefix attributes:

``` python
from chameleon_rs import PageTemplate

template = PageTemplate('<p data-tal-content="text">...</p>', enable_data_attributes=True)
```


# chameleon-rs and this reference

Every statement, expression type and built-in name described here behaves in chameleon-rs as it does in Chameleon; the examples on these pages were rendered with both engines to check. The engine underneath is different, which shows in three places, none of them in the template language itself:

- Chameleon compiles a template to Python source code, available as `template.source`. chameleon-rs compiles it to a native program; with `keep_source=True`, `template.source` is a readable dump of that program, not Python code.
- Custom expression types (classes that return Python AST) work through a bridge, at the speed of an ordinary Python call. Types that depend on Chameleon's compiler internals beyond that interface aren't supported. See [Custom expression types](expressions.md#custom-expression-types).
- Chameleon's `CHAMELEON_CACHE` (compiled templates cached on disk as Python modules) has no equivalent. Compiling is fast enough that it isn't needed.

See [Compatibility](../user-guide/compatibility.md) for how compatibility is measured.
