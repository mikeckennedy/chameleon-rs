## PageTemplate


Constructor for the page template language, rendered by the Rust VM.


Usage

``` python
PageTemplate()
```


Takes a string input as the only positional argument::

template = PageTemplate("


Hello, \${name}.


")

Output is of type `str`. See the module docs for the configuration keywords.

Every template has the builtin `render_partial(path, **kwargs)` (D10, see `chameleon_rs.partials`), which a render keyword or `extra_builtins` entry of the same name overrides.
