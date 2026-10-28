## PageTemplate.engine


Chameleon's expression engine factory, for code that evaluates expressions with


`PageTemplate.engine: Callable[[], ExpressionEngine]`


this template's types: `ExpressionEvaluator(template.engine, builtins)`, as z3c.pt does for `path()` and `exists()` in `python:` expressions. chameleon-rs compiles the template itself natively and doesn't use it.
