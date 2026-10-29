## TemplateError


Base class for compile-time template errors (`ParseError`, `CompilationError`, `ExpressionError`).


Usage

``` python
TemplateError()
```


Carries the error message (`args[0]`) and the offending source `token` (`args[1]`), with `filename`, `location` (`(line, column)`), and `offset` properties. Render-time exceptions are mixed with `chameleon_rs.exc.RenderError` instead.
