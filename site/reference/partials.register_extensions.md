## partials.register_extensions()


chameleon_partials' setup call: configures the folder [render_partial](render_partial.md#chameleon_rs.render_partial) uses (only


Usage

``` python
partials.register_extensions(
    template_folder, auto_reload=False, cache_init=True
)
```


that; [chameleon_rs.configure](configure.md#chameleon_rs.configure) also configures the framework integrations). A second call is ignored unless `cache_init` is false. Raises [PartialsException](partials.PartialsException.md#chameleon_rs.partials.PartialsException) for a missing folder.
