## compat.install()


Makes `import chameleon` (and its submodules) resolve to chameleon-rs and, with


Usage

``` python
compat.install(
    *,
    integrations=True,
)
```


`integrations` (the default), `import chameleon_partials` / `import chameleon_flask` / `import fastapi_chameleon` / `import chameleon_robyn` to the built-ins (see the module docs). Modules already imported under these names are replaced, with a `RuntimeWarning` naming them (code that imported them keeps its references). Idempotent; returns the module now known as [chameleon](shadow.ShadowDifference.md#chameleon_rs.shadow.ShadowDifference.chameleon).
