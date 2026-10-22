## PageTemplateFile


File-based constructor: the template is read from `filename` (or, with


Usage

``` python
PageTemplateFile()
```


`package_name`, from that package's resources) when first used.

File templates have the `load:` expression type, which loads templates relative to this one: `search_path` (a path or a sequence of paths; with `prepend_relative_search_path`, the default, the template's own directory comes first) and `loader_class` ([TemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader)), which gets the same configuration keywords.
