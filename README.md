# chameleon-rs

A Rust implementation of the [Chameleon](https://github.com/malthe/chameleon) page template
engine for Python, aiming to be a drop-in replacement: same template syntax, same output,
same API, much faster rendering.

Status: early development. See [docs/plan.md](docs/plan.md) for the build plan and
[docs/architecture.md](docs/architecture.md) for the design decisions.

Switching an application over is one line at startup, before anything imports Chameleon:

```python
import chameleon_rs.compat

chameleon_rs.compat.install()  # `import chameleon`, `chameleon_partials`, `chameleon_flask` give chameleon-rs
```

Built in, with no extra package:

- **Partials:** every template can call `${render_partial('shared/card.pt', item=item)}`; the
  partial renders straight into the page. `chameleon_rs.configure(template_folder='templates')`
  says where templates live (partials are also found next to the calling template and on its
  loader's search path). `chameleon_partials`' API (`register_extensions`, `extend_model`,
  `render_partial`) keeps working. See `chameleon_rs.partials`.
- **Flask and Quart:** `chameleon_rs.flask` has chameleon-flask's API (`configure` /
  `global_init`, `@template`, `render`, `response`, `not_found`, `error`, `clear`) and imports
  Flask only when it is imported itself. Views may return a dict, any mapping, a dataclass or
  a Pydantic model.

```sh
make dev     # set up venv with the reference Chameleon and build the extension
make test    # Rust tests, Python tests, Chameleon's own suite on the reference engine
```

Licensed under Chameleon's license (see `LICENSE.txt` and `COPYRIGHT.txt`); this project
ports Chameleon's behavior and reuses its test suite.
