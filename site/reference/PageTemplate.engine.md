## PageTemplate.engine


Chameleon's expression engine factory for this template's expression types.


`PageTemplate.engine: Callable[[], ExpressionEngine]`


Used by code that evaluates expressions dynamically, such as `ExpressionEvaluator(template.engine, builtins)` in z3c.pt for `path()` and `exists()` inside `python:` expressions. chameleon-rs compiles the template itself natively and doesn't use it.
