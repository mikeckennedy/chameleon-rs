# Inline syntax

> **Note**
>
> Adapted from [Chameleon's language reference](https://chameleon.readthedocs.io/) ([source](https://github.com/malthe/chameleon)), modified for chameleon-rs. Used under Chameleon's license; see `LICENSE.txt` and `COPYRIGHT.txt` in the repository.


# `${...}` operator

The `${...}` notation is short-hand for text insertion. The Python-expression inside the braces is evaluated and the result included in the output (all inserted text is escaped by default):

``` html

  ${content}

```

To escape this behavior, double the dollar sign: `$${...}` renders as the literal text `${...}`.

> **Note: Note**
>
> Older versions of Chameleon also accepted a backslash, `\${...}`. That is no longer supported (Chameleon 3.x changelog): the backslash is output as is and the expression is still evaluated.

Note that if an object implements the `__html__` method, the result of this method will be inserted as-is (without XML escaping).


# Code blocks

The `<?python ... ?>` notation allows you to embed Python code in templates:

``` html

  <?python numbers = map(str, range(1, 10)) ?>
  Please input a number from the range ${", ".join(numbers)}.

```

The scope of name assignments is up to the nearest macro definition, or the template, if macros are not used.

Note that code blocks can span multiple lines and start on the next line of where the processing instruction begins:

``` html
<?python
  foo = [1, 2, 3]
?>
```

You can use this to debug templates:

``` html

  <?python import pdb; pdb.set_trace() ?>

```


# Markup comments

You can apply the "!" and "?" modifiers to change how comments are processed:

Drop  
`<!--! This comment will be dropped from output -->`

Verbatim  
`<!--? This comment will be included verbatim -->` That is, evaluation of `${...}` expressions is disabled if the comment opens with the "?" character. The "?" itself is removed from the output.

In an ordinary comment, `${...}` expressions are evaluated.
