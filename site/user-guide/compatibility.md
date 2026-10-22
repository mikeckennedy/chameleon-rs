# Compatibility

chameleon-rs's goal is to produce exactly what Chameleon produces, for every template, and to raise the same errors. This page says how that's measured, and lists the few places where chameleon-rs knowingly differs.


# How it's measured

Each level is a check that runs on every change:

| Check | Result |
|----|----|
| **Chameleon's own test suite.** All 233 tests, vendored unmodified, run against chameleon-rs. | Every test that passes on Chameleon passes on chameleon-rs: 227 of 227. The other 6 depend on the test environment and are skipped on both engines. |
| **Fixture renders.** Every input template in Chameleon's test suite, rendered by both engines. | All 122 byte-identical. |
| **Differential fuzzing.** Generated templates, models and expressions, rendered by both engines and compared. | No differences in 20,000 examples per generator. |
| **Integration suites.** The test suites of fastapi-chameleon, chameleon-flask, chameleon-robyn and chameleon_partials, unmodified, on chameleon-rs. | All pass. |
| **Real sites.** Every page of two production Flask/Quart apps, crawled with both engines: anonymous and signed in, with all their partials and layouts. | Every page byte-identical. |

The upstream suite and the fuzzers compare output byte for byte, and they also compare the type and message of any error a template raises.


# What's identical

- **The template language**: TAL, METAL, i18n, `${...}`, every expression type, and whitespace and attribute handling, down to the byte.
- **The Python API**: the template classes and their options, [PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader), macros, `translate` functions, `tal:on-error` handlers, and `CHAMELEON_*` environment variables.
- **The modules**: [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install) maps each Chameleon module to the chameleon-rs module of the same name.
- **Errors**: the same exception types, and the same formatted messages with the template position and expression.
- **The helper libraries**: chameleon_partials' and chameleon-flask's APIs, built in.


# Known differences

These are deliberate. Each comes from chameleon-rs compiling to a native program rather than to Python source code.

| Area | Chameleon | chameleon-rs |
|----|----|----|
| `template.source` | The generated Python module's code. | With `keep_source=True`, a readable dump of the compiled program. It isn't Python. |
| Custom expression types | A class whose compiler returns Python AST statements. | Supported through a bridge that wraps the AST in a Python function, at the speed of an ordinary Python call. Types that depend on Chameleon's compiler internals beyond that interface aren't supported. |
| `CHAMELEON_CACHE` | Writes the generated modules to disk and loads them on the next start. | Checks the directory but writes nothing. Compiling is about 70x faster than Chameleon, so there's little to cache. |
| Code-generation modules | `chameleon.nodes`, `chameleon.program`, `chameleon.zpt.program`, `chameleon.benchmark`. | Not provided, since chameleon-rs doesn't generate code. Importing them raises `ModuleNotFoundError`. |

The built-in partials have one behaviour change, which you can turn off: a path that isn't in the configured template folder is also looked for next to the calling template, where chameleon_partials would raise. `configure(partial_search='registered')` restores the original lookup. See [Partials](partials.md#how-a-path-is-found).


# Found a difference?

If a template renders differently on chameleon-rs than on Chameleon, that's a bug. Please [open an issue](https://github.com/mikeckennedy/chameleon-rust/issues) with the template, the model, and both outputs. [Shadow mode](switching-from-chameleon.md#check-your-own-app-first-shadow-mode) can find such templates in a running app.
