# Ported from Chameleon's src/chameleon/loader.py (`cache`, `import_package_resource`,
# `TemplateLoader`), modified: type annotations; the compiled-module loaders
# (`MemoryLoader`, `ModuleLoader`) do not apply to chameleon-rs.
"""Template loading by path spec: what the ``load:`` expression type calls (as the
``__loader`` template builtin of file templates) and what Phase 7's ``PageTemplateLoader``
builds on."""

from __future__ import annotations

import contextlib
import functools
import importlib.resources
import os
import posixpath
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, cast


def cache[F: Callable[..., Any]](func: F) -> F:
    def load(self: Any, *args: Any, **kwargs: Any) -> Any:
        template = self.registry.get(args)
        if template is None:
            self.registry[args] = template = func(self, *args, **kwargs)
        return template

    return cast('F', load)


@contextlib.contextmanager
def import_package_resource(name: str) -> Iterator[Path]:
    path: Path = importlib.resources.files(name)  # type: ignore[assignment]
    yield path
    if hasattr(path.root, 'close'):
        path.root.close()


class TemplateLoader:
    """Template loader class.

    To load templates using relative filenames, pass a sequence of paths (or a single path)
    as ``search_path``.

    To apply a default filename extension to inputs which do not have an extension already
    (i.e. no dot), provide this as ``default_extension`` (e.g. ``'.pt'``).

    Additional keyword-arguments will be passed on to the template constructor.
    """

    default_extension: str | None = None
    registry: dict[tuple[Any, ...], Any]

    def __init__(
        self,
        search_path: Sequence[str] | str | None = None,
        default_extension: str | None = None,
        **kwargs: Any,
    ) -> None:
        if search_path is None:
            search_path = []
        if isinstance(search_path, str):
            search_path = [search_path]
        if default_extension is not None:
            self.default_extension = '.' + default_extension.lstrip('.')
        self.search_path = search_path
        self.registry = {}
        self.kwargs = kwargs

    @cache
    def load(self, spec: str, cls: Any = None) -> Any:
        if cls is None:
            raise ValueError('Unbound template loader.')

        spec = spec.strip()

        if self.default_extension is not None and '.' not in spec:
            spec += self.default_extension

        package_name = None

        if not os.path.isabs(spec):
            if ':' in spec:
                package_name, spec = spec.split(':', 1)
            else:
                for path in self.search_path:
                    if not os.path.isabs(path) and ':' in path:
                        package_name, path = path.split(':', 1)
                        with import_package_resource(package_name) as files:
                            if files.joinpath(path).joinpath(spec).exists():
                                spec = posixpath.join(path, spec)
                                break
                    else:
                        path = os.path.join(path, spec)
                        if os.path.exists(path):
                            package_name = None
                            spec = path
                            break
                else:
                    raise ValueError(f'Template not found: {spec}.')

        return cls(spec, search_path=self.search_path, package_name=package_name, **self.kwargs)

    def bind(self, cls: Any) -> Callable[[str], Any]:
        return functools.partial(self.load, cls=cls)
