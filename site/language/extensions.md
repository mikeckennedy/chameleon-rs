# Extensions and differences

> **Note**
>
> Adapted from [Chameleon's language reference](https://chameleon.readthedocs.io/) ([source](https://github.com/malthe/chameleon)), modified for chameleon-rs. Used under Chameleon's license; see `LICENSE.txt` and `COPYRIGHT.txt` in the repository.


# Language extensions

Chameleon extends the *page template* language with new expression types and language features. Some take inspiration from [Genshi](https://genshi.edgewall.org/). chameleon-rs supports all of them.


## New expression types

The [structure](expressions.md#structure-expression) expression wraps an expression result as *structure*:

``` html
<div>${structure: body.text}</div>
```

The [import](expressions.md#import-expression) expression imports module globals:

``` html

  ...

```

The [load](expressions.md#load-expression) expression loads templates relative to the current template:

``` html

  ...

```


## Tuple unpacking

The `tal:define` and `tal:repeat` statements support tuple unpacking:

``` html
tal:define="(a, b, c) [1, 2, 3]"
```

Extended [iterable unpacking](https://peps.python.org/pep-3132/) using the asterisk character is not currently supported (even for versions of Python that support it natively).


## Dictionary lookup as fallback after attribute error

If attribute lookup (using the `obj.<name>` syntax) raises an `AttributeError` exception, a secondary lookup is attempted using dictionary lookup: `obj['<name>']`.

Chameleon does this by rewriting all attribute lookups to a custom lookup call, and chameleon-rs does the same lookup natively:

``` python
def lookup_attr(obj, key):
    try:
        return getattr(obj, key)
    except AttributeError as exc:
        try:
            get = obj.__getitem__
        except AttributeError:
            raise exc
        try:
            return get(key)
        except KeyError:
            raise exc
```


## Inline string substitution

In element attributes and in the text or tail of an element, string expression interpolation is available using the `${...}` syntax:

``` html
<span class="content-${item_type}">
   ${title or item_id}
</span>
```


## Code blocks

Using `<?python ... ?>` notation, you can embed Python statements in your templates:

``` html

  <?python numbers = map(str, range(1, 10)) ?>
  Please input a number from the range ${", ".join(numbers)}.

```


## Literal content

While the `tal:content` and `tal:replace` attributes both support the `structure` keyword which inserts the content as a literal (without XML-escape), an object may also provide an `__html__` method to the same effect.

The result of the method will be inserted as *structure*.


## Switch statement

Two new attributes have been added: `tal:switch` and `tal:case`. A case attribute works like a condition and only allows content if the value matches that of the nearest parent switch value.


## Partials (chameleon-rs)

chameleon-rs adds a built-in [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) function to every template, which renders another template file into the page:

``` html

  ${render_partial('shared/card.pt', item=item)}

```

It's the API of the [chameleon_partials](https://github.com/mikeckennedy/chameleon_partials) package, built in. See [Partials](../user-guide/partials.md).


# Incompatibilities and differences

There are a number of incompatibilities and differences between the Chameleon language implementation and the Zope reference implementation (ZPT). chameleon-rs follows Chameleon in each of them.


## Default expression

The default expression type is Python.


## Template arguments

Arguments passed by keyword to the render- or call method are inserted directly into the template execution namespace. This is different from ZPT where these are only available through the `options` dictionary.

Zope:

``` html

```

Chameleon:

``` html

```


## Special symbols

The `CONTEXTS` symbol is not available.

The [z3c.pt](https://pypi.org/project/z3c.pt/) package works as a compatibility layer for Chameleon. The template classes in this package provide an implementation which is fully compatible with ZPT.
