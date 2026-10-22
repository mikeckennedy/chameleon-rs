## PageTextTemplate


Text-based template class: only `${...}` interpolation, no markup, nothing escaped.


Usage

``` python
PageTextTemplate(
    body,
    **config,
)
```


Takes a non-XML input::

template = PageTextTemplate("Hello, \${name}.")
