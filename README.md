# chameleon-rs

A Rust implementation of the [Chameleon](https://github.com/malthe/chameleon) page template
engine for Python, aiming to be a drop-in replacement: same template syntax, same output,
same API, much faster rendering.

Status: early development. See [docs/plan.md](docs/plan.md) for the build plan and
[docs/architecture.md](docs/architecture.md) for the design decisions.

```sh
make dev     # set up venv with the reference Chameleon and build the extension
make test    # Rust tests, Python tests, Chameleon's own suite on the reference engine
```

Licensed under Chameleon's license (see `LICENSE.txt` and `COPYRIGHT.txt`); this project
ports Chameleon's behavior and reuses its test suite.
