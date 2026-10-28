# Ported from Chameleon's src/chameleon/loader.py (`cache`, `import_package_resource`,
# `TemplateLoader`, `MemoryLoader`, `ModuleLoader`), modified: type annotations; `cache`
# stores with `setdefault`, so concurrent loads share one template; search paths and specs
# may be `os.PathLike` (malthe/chameleon#342). chameleon-rs
# does not compile templates into Python modules, so `MemoryLoader` and `ModuleLoader` are
# kept only for code that uses them directly (and as a template's `loader` attribute).
"""Template loading by path spec: what the ``load:`` expression type calls (as the
``__loader`` template builtin of file templates) and what ``PageTemplateLoader`` builds on."""

from __future__ import annotations

import contextlib
import functools
import importlib.resources
import logging
import os
import posixpath
import py_compile
import shutil
import sys
import tempfile
import warnings
from collections.abc import Callable, Generator, Iterable, Sequence
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from threading import RLock
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

lock = RLock()
acquire_lock = lock.acquire
release_lock = lock.release
del lock

log = logging.getLogger('chameleon.loader')


def search_paths(search_path: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None) -> list[str]:
    """A search path argument as a list of strings: one path or a sequence of them, each a
    string or an ``os.PathLike`` (malthe/chameleon#342; Chameleon accepts only strings)."""
    if search_path is None:
        return []
    if isinstance(search_path, (str, os.PathLike)):
        search_path = [search_path]
    return [os.fspath(path) for path in search_path]


def cache[F: Callable[..., Any]](func: F) -> F:
    def load(self: Any, *args: Any, **kwargs: Any) -> Any:
        template = self.registry.get(args)
        if template is None:
            # Threads loading the same spec at once may each create a template; `setdefault`
            # (atomic on a dict, free-threaded builds included) makes them all use the first.
            template = self.registry.setdefault(args, func(self, *args, **kwargs))
        return template

    return cast('F', load)


@contextlib.contextmanager
def import_package_resource(name: str) -> Generator[Traversable, None, None]:
    path = importlib.resources.files(name)
    yield path
    # A `pathlib.Path` or a `zipfile.Path` (whose `root`, a zip file, is closed): `root` is not
    # part of the `Traversable` protocol. A namespace package's `MultiplexedPath` has no `root`,
    # so this raises `AttributeError` for one: as in Chameleon ("FIXME: we should restrict
    # ourselves to the Traversable protocol"), kept for parity.
    root = path.root  # ty: ignore[unresolved-attribute]
    if hasattr(root, 'close'):
        root.close()


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
        search_path: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
        default_extension: str | None = None,
        **kwargs: Any,
    ) -> None:
        # A list or tuple of strings is kept as is, as in Chameleon (code may append to
        # `loader.search_path` later).
        paths: Sequence[str]
        if isinstance(search_path, (list, tuple)) and all(isinstance(path, str) for path in search_path):
            paths = cast('Sequence[str]', search_path)
        else:
            paths = search_paths(search_path)
        if default_extension is not None:
            self.default_extension = '.' + default_extension.lstrip('.')
        self.search_path = paths
        self.registry = {}
        self.kwargs = kwargs

    @cache
    def load(self, spec: str | os.PathLike[str], cls: Any = None) -> Any:
        if cls is None:
            raise ValueError('Unbound template loader.')

        spec = os.fspath(spec).strip()

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
                            # `exists` (a `pathlib.Path` or `zipfile.Path` method) is not part
                            # of the `Traversable` protocol (a `MultiplexedPath` lacks it: as in
                            # Chameleon, see `import_package_resource`).
                            if files.joinpath(path).joinpath(spec).exists():  # ty: ignore[unresolved-attribute]
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


class MemoryLoader:
    def build(self, source: str, filename: str) -> dict[str, Any]:
        code = compile(source, filename, 'exec')
        env: dict[str, Any] = {}
        exec(code, env)  # noqa: S102 - Chameleon's module loader
        return env

    def get(self, name: str) -> None:
        return None


class ModuleLoader:
    def __init__(self, path: str, remove: bool = False) -> None:
        self.path = path
        self.remove = remove

    def __del__(self, shutil: Any = shutil) -> None:
        if not self.remove:
            return
        try:
            shutil.rmtree(self.path)
        except BaseException:  # noqa: BLE001 - as in Chameleon
            warnings.warn(f'Could not clean up temporary file path: {self.path}', stacklevel=1)

    def get(self, filename: str) -> dict[str, Any] | None:
        path = os.path.join(self.path, filename)
        if os.path.exists(path):
            log.debug('loading module from cache: %s.', filename)
            base, _ext = os.path.splitext(filename)
            return self._load(base, path)
        log.debug('cache miss: %s', filename)
        return None

    def build(self, source: str, filename: str) -> dict[str, Any]:
        acquire_lock()
        try:
            base, _ext = os.path.splitext(filename)
            name = os.path.join(self.path, base + '.py')

            log.debug('writing source to disk (%d bytes).', len(source))
            fd, fn = tempfile.mkstemp(prefix=base, suffix='.tmp', dir=self.path)
            temp = os.fdopen(fd, 'wb')
            encoded = source.encode('utf-8')
            header = b'# -*- coding: utf-8 -*-\n'

            try:
                try:
                    temp.write(header)
                    temp.write(encoded)
                finally:
                    temp.close()
            except BaseException:
                os.remove(fn)
                raise

            os.rename(fn, name)
            log.debug('compiling %s into byte-code...', filename)
            py_compile.compile(name)

            return self._load(base, name)
        finally:
            release_lock()

    def _load(self, base: str, filename: str) -> dict[str, Any]:
        acquire_lock()
        try:
            module = sys.modules.get(base)
            if module is None:
                loader = SourceFileLoader(base, filename)
                spec = spec_from_loader(base, loader)
                if spec is None:
                    raise ModuleNotFoundError(f'{base} ({filename})')
                module = module_from_spec(spec)
                loader.exec_module(module)
                sys.modules[base] = module
        finally:
            release_lock()

        return module.__dict__
