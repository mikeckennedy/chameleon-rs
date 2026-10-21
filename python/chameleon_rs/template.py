# Ported from Chameleon's src/chameleon/zpt/template.py (PageTemplate.render's setup of
# `__translate`, `__decode`, `__on_error_handler`, `target_language` and `repeat`;
# `Macros`, `PageTemplate.include` and `__getitem__`;
# `PageTemplate.parse`, `expression_types`, `_builtins`, `BOOLEAN_HTML_ATTRIBUTES`,
# `PageTextTemplate`, `PageTemplateFile` with its `load:` type and relative loader,
# `PageTextTemplateFile`) and template.py (`BaseTemplate.write`, `extra_builtins`,
# `BaseTemplateFile.__init__` / `cook_check` / `mtime` / `read`), modified: the template
# compiles to a native program instead of a Python module.
"""The public template classes (D7): ``PageTemplate(body)``, ``PageTextTemplate(body)`` and
the file-based ``PageTemplateFile(filename)`` / ``PageTextTemplateFile(filename)``."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Mapping
from functools import partial
from typing import Any, ClassVar

from chameleon_rs._native import Macro, Program
from chameleon_rs.i18n import simple_translate
from chameleon_rs.loader import TemplateLoader, import_package_resource
from chameleon_rs.tal import RepeatDict
from chameleon_rs.tales import (
    ExistsExpr,
    ImportExpr,
    NotExpr,
    ProxyExpr,
    PythonExpr,
    StringExpr,
    StructureExpr,
    native_types,
)
from chameleon_rs.utils import detect_encoding, read_bytes, read_xml_encoding

BOOLEAN_HTML_ATTRIBUTES = [
    # From http://www.w3.org/TR/xhtml1/#guidelines (C.10)
    'compact',
    'nowrap',
    'ismap',
    'declare',
    'noshade',
    'checked',
    'disabled',
    'readonly',
    'multiple',
    'selected',
    'noresize',
    'defer',
]

# Settings that change the compiled program, passed to the native `Program`.
PROGRAM_OPTIONS = (
    'implicit_i18n_translate',
    'implicit_i18n_attributes',
    'trim_attribute_space',
    'enable_data_attributes',
    'enable_comment_interpolation',
    'restricted_namespace',
    'strict',
)


class Macros:
    """The ``macros`` template builtin: ``macros['name']`` is the macro defined with
    ``metal:define-macro="name"``."""

    __slots__ = ('_cache', 'template')

    def __init__(self, template: PageTemplate) -> None:
        self.template = template
        # name -> (program, macro): a lookup is cached until the template is compiled again.
        self._cache: dict[str, tuple[Program, Macro]] = {}

    def __getitem__(self, name: str) -> Macro:
        # Chameleon looks the function up as `_render_<name>`, with `-` replaced; it defined
        # each macro's function under its name with every non-word character replaced.
        name = name.replace('-', '_')
        template = self.template
        template.cook_check()
        program = template._program
        cached = self._cache.get(name)
        if cached is not None and cached[0] is program:
            return cached[1]
        macro = Macro(program, name, template._builtin_dict)
        self._cache[name] = (program, macro)
        return macro

    @property
    def names(self) -> list[str]:
        self.template.cook_check()
        return list(self.template._program.macro_names)


class PageTemplate:
    """A page template compiled from a string, rendered by the Rust VM.

    ``partials`` is a temporary prototype of the built-in ``render_partial`` (D10): a mapping
    from names to other ``PageTemplate`` objects, which ``render_partial(name, **kwargs)``
    renders as native frames when no ``render_partial`` is passed to ``render``. Phase 9
    replaces it with path resolution.
    """

    expression_types: ClassVar[dict[str, Any]] = {
        'python': PythonExpr,
        'string': StringExpr,
        'not': NotExpr,
        'exists': ExistsExpr,
        'import': ImportExpr,
        'structure': StructureExpr,
    }
    default_expression = 'python'

    translate: ClassVar[Callable[..., Any]] = staticmethod(simple_translate)
    on_error_handler: Callable[[BaseException], object] | None = None
    encoding: str | None = None

    # Updated into the template builtins at compile time (Chameleon's `extra_builtins`).
    extra_builtins: ClassVar[dict[str, Any]] = {}

    mode = 'xml'
    default_encoding = 'utf-8'
    default_content_type = 'text/html'
    boolean_attributes: Any = None
    implicit_i18n_translate = False
    implicit_i18n_attributes: Any = frozenset()
    trim_attribute_space = False
    enable_data_attributes = False
    enable_comment_interpolation = True
    restricted_namespace = True
    strict = True
    filename = '<string>'

    def __init__(
        self,
        body: str | bytes,
        *,
        filename: str = '<string>',
        partials: Mapping[str, PageTemplate] | None = None,
        **config: Any,
    ) -> None:
        # Like Chameleon's `BaseTemplate.__init__`: configuration becomes instance attributes.
        self.__dict__.update(config)
        self.filename = filename
        self._setup(partials)
        self.write(body)

    def _setup(self, partials: Mapping[str, PageTemplate] | None) -> None:
        self.macros = Macros(self)
        self._partials = partials
        self._defaults = self._render_defaults(None, None)

    @property
    def builtins(self) -> dict[str, Any]:
        """The template builtins: names every expression can use (a render keyword of the
        same name overrides them)."""
        return self._builtins()

    def _builtins(self) -> dict[str, Any]:
        return {'template': self, 'macros': self.macros, 'nothing': None}

    def _builtin_values(self) -> dict[str, Any]:
        values = self._builtins()
        values.update(self.extra_builtins)
        return values

    def write(self, body: str | bytes) -> None:
        """Sniff the content type and encoding (`BaseTemplate.write`), then compile."""
        encoding: str | None
        if isinstance(body, bytes):
            body, encoding, content_type = read_bytes(body, self.default_encoding)
        elif body.startswith('<?xml'):
            content_type = 'text/xml'
            encoding = read_xml_encoding(body.encode('utf-8'))
        else:
            content_type, encoding = detect_encoding(body, self.default_encoding)
        self._cook(body, content_type, encoding)

    def _cook(self, body: str, content_type: str | None, encoding: str | None) -> None:
        self.content_type = content_type or self.default_content_type
        self.content_encoding = encoding
        self.body = body
        # Like Chameleon's `cook`, the builtins are bound when the template compiles.
        self._builtin_dict = self._builtin_values()
        self._program = self._compile(body)

    def _compile(self, body: str) -> Program:
        """`PageTemplate.parse`: HTML boolean attributes and newline normalization unless the
        content type is XML."""
        boolean_attributes = self.boolean_attributes
        if self.content_type != 'text/xml':
            if boolean_attributes is None:
                boolean_attributes = BOOLEAN_HTML_ATTRIBUTES
            body = body.replace('\r\n', '\n').replace('\r', '\n')
        options = {name: getattr(self, name) for name in PROGRAM_OPTIONS}
        return Program(
            body,
            str(self.filename),
            expression_types=native_types(self.expression_types),
            default_expression=self.default_expression,
            builtins=sorted(self._builtin_dict),
            fast_path=self.__dict__.get('_fast_path', True),
            mode=self.mode,
            boolean_attributes=boolean_attributes or (),
            **options,
        )

    def cook_check(self) -> bool:
        """Compiled at construction: nothing to do (file templates compile on first use)."""
        return False

    def __getitem__(self, name: str) -> Macro:
        return self.macros[name]

    @property
    def include(self) -> Macro:
        """The whole template as a macro (``metal:use-macro="template"``): Chameleon's
        ``include(stream, econtext, rcontext, ...)``, which renders the main function into
        ``stream``."""
        self.cook_check()
        return Macro(self._program, None, self._builtin_dict)

    def _partial_programs(self) -> dict[str, Program] | None:
        if self._partials is None:
            return None
        programs = {}
        for name, template in self._partials.items():
            template.cook_check()
            programs[name] = template._program
        return programs

    def _render_defaults(self, encoding: str | None, translate: Callable[..., Any] | None) -> dict[str, Any]:
        if translate is None:
            translate = self.translate
            if translate is None:
                translate = type(self).translate

        encoding = encoding if encoding is not None else self.encoding
        decode: Callable[[bytes], str]
        if encoding is not None:
            txl = translate

            def translate(
                msgid: str | bytes, txl: Callable[..., Any] = txl, encoding: str = encoding, **kwargs: Any
            ) -> str:
                if isinstance(msgid, bytes):
                    msgid = bytes.decode(msgid, encoding)
                return txl(msgid, **kwargs)

            def decode(inst: bytes, encoding: str = encoding) -> str:
                return bytes.decode(inst, encoding, 'ignore')
        else:
            decode = bytes.decode

        return {
            '__translate': translate,
            '__decode': decode,
            '__on_error_handler': self.on_error_handler,
            'target_language': None,
        }

    def render(self, encoding: str | None = None, **kw: Any) -> str:
        self.cook_check()
        translate = kw.get('translate')
        if encoding is None and translate is None:
            defaults = self._defaults
        else:
            defaults = self._render_defaults(encoding, translate)
        for key, value in defaults.items():
            kw.setdefault(key, value)
        if 'repeat' not in kw:
            kw['repeat'] = RepeatDict({})
        partials = self._partial_programs()
        return self._program.render(kw, partials, defaults, self._builtin_dict)

    def __call__(self, **kw: Any) -> str:
        # A method of its own, as `BaseTemplate.__call__` is: the interpreter's stack depth at
        # the render matters for where deep macro recursion fails (`Errors::frame_budget`).
        return self.render(**kw)

    def __repr__(self) -> str:
        return f'<{type(self).__name__} {self.filename}>'


class PageTextTemplate(PageTemplate):
    """A text template: only `${...}` interpolation, no markup, nothing escaped."""

    mode = 'text'


class PageTemplateFile(PageTemplate):
    """A page template read from a file, compiled on first use (and again when it changed,
    with ``auto_reload``). It has the ``load:`` expression type, which loads templates
    relative to this one (``search_path`` and ``package_name`` as in Chameleon).

    Minimal until Phase 7, which completes the loader semantics (``PageTemplateLoader``,
    ``default_extension`` handling across classes, ``CHAMELEON_*`` settings).
    """

    expression_types: ClassVar[dict[str, Any]] = {
        **PageTemplate.expression_types,
        'load': partial(ProxyExpr, '__loader', ignore_prefix=False),
    }

    prepend_relative_search_path = True
    auto_reload = False

    def __init__(
        self,
        filename: str | os.PathLike[str],
        loader_class: type[TemplateLoader] = TemplateLoader,
        package_name: str | None = None,
        search_path: Iterable[str] | str | None = None,
        auto_reload: bool | None = None,
        *,
        partials: Mapping[str, PageTemplate] | None = None,
        **config: Any,
    ) -> None:
        if search_path is None:
            paths: list[str] = []
        elif isinstance(search_path, str):
            paths = [search_path]
        else:
            paths = list(search_path)

        if package_name is None:
            filename = os.path.abspath(os.path.normpath(os.path.expanduser(filename)))
        self.package_name = package_name
        self.filename = str(filename)
        if auto_reload is not None:
            self.auto_reload = auto_reload
        self.__dict__.update(config)
        self._lock = threading.Lock()
        self._cooked = False
        self._v_last_read: float | None = None
        self._setup(partials)

        if self.prepend_relative_search_path:
            path = os.path.dirname(self.filename)
            if package_name is not None:
                path = f'{package_name}:{path}'
            paths.insert(0, path)
        loader = loader_class(search_path=paths, **config)
        self._loader = loader.bind(type(self))

    def _builtins(self) -> dict[str, Any]:
        builtins = super()._builtins()
        builtins['__loader'] = self._loader
        return builtins

    def cook_check(self) -> bool:
        if self.auto_reload:
            mtime = self.mtime()
            if mtime != self._v_last_read:
                self._v_last_read = mtime
                self._cooked = False
        if self._cooked:
            return False
        with self._lock:
            if self._cooked:
                return False
            body, encoding, content_type = read_bytes(self.read(), self.default_encoding)
            self._cook(body, content_type, encoding)
            self._cooked = True
        return True

    def mtime(self) -> float:
        if self.package_name is not None:
            with import_package_resource(self.package_name) as files:
                return float(files.joinpath(self.filename).stat().st_mtime)
        try:
            return os.path.getmtime(self.filename)
        except OSError:
            return 0

    def read(self) -> bytes:
        if self.package_name is not None:
            with import_package_resource(self.package_name) as files:
                return files.joinpath(self.filename).read_bytes()
        with open(self.filename, 'rb') as f:
            return f.read()


class PageTextTemplateFile(PageTemplateFile):
    """A text template read from a file. Rendering returns ``bytes``, encoded with the
    template's ``encoding`` (UTF-8 by default), as Chameleon's does."""

    mode = 'text'

    def render(self, encoding: str | None = None, **kw: Any) -> bytes:  # type: ignore[override]
        result = super().render(encoding, **kw)
        return result.encode(self.encoding or 'utf-8')

    def __call__(self, **kw: Any) -> bytes:  # type: ignore[override]
        return self.render(**kw)
