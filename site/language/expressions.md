# Expressions (TALES)

> **Note**
>
> Adapted from [Chameleon's language reference](https://chameleon.readthedocs.io/) ([source](https://github.com/malthe/chameleon)), modified for chameleon-rs. Used under Chameleon's license; see `LICENSE.txt` and `COPYRIGHT.txt` in the repository.

The *Template Attribute Language Expression Syntax* (TALES) standard describes expressions that supply [TAL](tal.md) and [METAL](macros.md) with data. TALES is *one* possible expression syntax for these languages, but they are not bound to this definition. Similarly, TALES could be used in a context having nothing to do with TAL or METAL.

TALES expressions are described below with any delimiter or quote markup from higher language layers removed. Here is the basic definition of TALES syntax:

``` text
Expression  ::= [type_prefix ':'] String
type_prefix ::= Name
```

Here are some simple examples:

``` text
1 + 2
None
string:Hello, ${view.user_name}
```

The optional *type prefix* determines the semantics and syntax of the *expression string* that follows it. A given implementation of TALES can define any number of expression types, with whatever syntax you like. It also determines which expression type is indicated by omitting the prefix.


# Types

These are the available TALES expression types:

| Prefix | Description |
|----|----|
| `exists` | Evaluate the result inside an exception handler; if one of the exceptions `AttributeError`, `LookupError`, `TypeError`, `NameError`, or `KeyError` is raised during evaluation, the result is `False`, otherwise `True`. Note that the original result is discarded in any case. |
| `import` | Import a global symbol using dotted notation. |
| [load](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader.load) | Load a template relative to the current template or absolute. |
| `not` | Negate the expression result. |
| `python` | Evaluate a Python expression. |
| `string` | Format a string. |
| `structure` | Wraps the expression result as *structure*. |

> **Note: Note**
>
> The default expression type is `python`.

> **Warning: Warning**
>
> The Zope reference engine defaults to a `path` expression type, which is closely tied to the Zope framework. This expression is not implemented in Chameleon (but it's available in a Zope framework compatibility package, [z3c.pt](https://pypi.org/project/z3c.pt/)).

> **Note: Note**
>
> **chameleon-rs:** custom expression types (classes registered in `expression_types` that return Python AST) are supported through a bridge that wraps their AST in a Python function, so they run at the speed of an ordinary Python call. Types that depend on Chameleon's compiler internals beyond that interface aren't supported. See [Custom expression types](#custom-expression-types).

There's a mechanism to allow fallback to alternative expressions, if one should fail (raise an exception). The pipe character ('\|') is used to separate two expressions:

``` html

```

This mechanism applies only to the `python` expression type, and by derivation `string`.


# `python`

Evaluates a Python expression.


## Syntax

Python expression syntax:

``` text
Any valid Python language expression
```


## Description

Python expressions are executed natively within the template. There is no built-in security apparatus.

> **Note: Note**
>
> **chameleon-rs:** Chameleon translates the whole template, expressions included, into Python source code. chameleon-rs compiles the template into a native program; each expression is either evaluated by a native fast path with the same semantics or compiled by CPython. Either way it's ordinary Python, with access to everything in scope.


# `string`


## Syntax

String expression syntax:

``` text
string_expression ::= ( plain_string | [ varsub ] )*
varsub            ::= ( '$' Variable ) | ( '${ Expression }' )
plain_string      ::= ( '$$' | non_dollar )*
non_dollar        ::= any character except '$'
```


## Description

String expressions interpret the expression string as text. If no expression string is supplied the resulting string is *empty*. The string can contain variable substitutions of the form `$name` or `${expression}`, where `name` is a variable name, and `expression` is a TALES-expression. The escaped string value of the expression is inserted into the string.

> **Note: Note**
>
> To prevent a `$` from being interpreted this way, it must be escaped as `$$`. Using a backslash-escape is not supported.


## Examples

Basic string formatting:

``` html
<span tal:replace="string:$this and $that">
  Spam and Eggs
</span>

<p tal:content="string:${request.form['total']}">
  total: 12
</p>
```

Including a dollar sign:

``` html
<p tal:content="string:$$$cost">
  cost: $42.00
</p>
```


# `import`

Imports a module global:

``` html

  ...

```


# `structure`

Wraps the expression result as *structure*: The replacement text is inserted into the document without escaping, allowing HTML/XML markup to be inserted. This can break your page if the text contains unanticipated markup (eg. text submitted via a web form), which is the reason that it is not the default.

``` html
<div>${structure: body.text}</div>
```


# [load](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader.load)

Loads a template instance.


## Syntax

Load expression syntax:

``` text
Relative or absolute file path
```


## Description

The template will be loaded using the same template class as the calling template.


## Examples

Loading a template and using it as a macro:

``` html

```


# Custom expression types

A template class's `expression_types` maps each prefix to a type. Add your own by extending the mapping:

``` python
from chameleon_rs import PageTemplate


class MyTemplate(PageTemplate):
    expression_types = {**PageTemplate.expression_types, 'upper': UpperExpr}
```

A type is called with the expression text (without the prefix), and the result is called with `(target, engine)`. It returns a list of Python AST statements that assign the value to `target`. There are two ways to write one.


## Subclass `TalesExpr`

`chameleon_rs.tales.TalesExpr` is Chameleon's base class (`chameleon.tales.TalesExpr`). A subclass implements `translate(expression, target)` for one expression, and the base class adds the `|` fallbacks:

``` python
import ast

from chameleon_rs.codegen import template
from chameleon_rs.exc import ExpressionError
from chameleon_rs.tales import TalesExpr


class UpperExpr(TalesExpr):
    """``upper: name``: the value of ``name``, upper-cased."""

    def translate(self, expression, target):
        name = expression.strip()
        if not name.isidentifier():
            raise ExpressionError('expected a name', expression)
        return template(
            'target = str(name).upper()',
            target=target,
            name=ast.Name(id=name, ctx=ast.Load()),
        )
```

`<p tal:content="upper: title"/>` renders `<p>HALLO</p>` for `title='hallo'`, and `upper: missing | title` falls back to `title`. Names in the AST are looked up in the template's variables, as in a `python:` expression.


## Delegate to the engine

A type that rewrites its text into another expression can let the engine compile it. It doesn't need a base class:

``` python
class TwiceExpr:
    def __init__(self, expression):
        self.expression = expression

    def __call__(self, target, engine):
        return engine.parse(self.expression).assign_value(target)
```

`engine.parse(text)` compiles `text` with the template's own expression types, so `twice: python: title * 2` works. `assign_value(target)` assigns its value; `assign_text` and `assign_bool` assign it converted to text or to a boolean.


## Rejecting text it can't compile

A type must raise `ExpressionError` for text it can't compile. Either chameleon-rs' class (`chameleon_rs.exc.ExpressionError`) or Chameleon's (`chameleon.exc.ExpressionError`) works.

This matters for `${...}`. The closing brace can't be found by scanning, because an expression can contain braces of its own. So for `class="${upper: a} ${upper: b}"` the type first gets the longest candidate, `a} ${upper: b`, and then shorter ones until it accepts one. A type that accepts anything takes the first candidate, and the rest of the attribute disappears without an error. Chameleon works the same way.


## What isn't supported

Chameleon's compiler internals (`chameleon.nodes`, `chameleon.program`, `chameleon.zpt.program`) don't exist in chameleon-rs, and types built on them don't work.


# Built-in names

These are the names always available in the TALES expression namespace:

- `default`: special value used to specify that existing text or attributes should not be replaced. See the documentation for individual TAL statements for details on how they interpret *default*.
- `repeat`: the *repeat* variables; see [`tal:repeat`](tal.md#tal-repeat) for more information.
- [template](../reference/flask.template.md#chameleon_rs.flask.template): reference to the template which was first called; this symbol is carried over when using macros.
- `macros`: reference to the macros dictionary that corresponds to the current template.
- `nothing`: the value `None`.

chameleon-rs adds one more:

- [render_partial](../reference/render_partial.md#chameleon_rs.render_partial): renders another template file into the page, as in `${render_partial('shared/card.pt', item=item)}`. See [Partials](../user-guide/partials.md).
