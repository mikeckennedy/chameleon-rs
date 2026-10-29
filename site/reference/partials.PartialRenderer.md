## partials.PartialRenderer


Renders a partial template by path and returns an [HTML](partials.HTML.md#chameleon_rs.partials.HTML) object.


Usage

``` python
partials.PartialRenderer()
```


Used for [chameleon_rs.render_partial](render_partial.md#chameleon_rs.render_partial) (no calling template) and a template's own [render_partial](render_partial.md#chameleon_rs.render_partial) builtin (the calling template for `partial_search='caller'`). Written as a whole `${render_partial(...)}` expression it is not called at all: the partial renders as a native frame.


## Attributes

| Name | Description |
|----|----|
| [template](#template) | The template this [render_partial](render_partial.md#chameleon_rs.render_partial) resolves relative to (`None`: none). |

------------------------------------------------------------------------


#### template


The template this [render_partial](render_partial.md#chameleon_rs.render_partial) resolves relative to (`None`: none).


`template: Any`
