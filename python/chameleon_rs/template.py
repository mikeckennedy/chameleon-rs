# Ported from Chameleon's src/chameleon/template.py (`BaseTemplate`, `BaseTemplateFile`,
# `get_package_versions`, `get_pkg_digest`, `_make_module_loader`) and zpt/template.py
# (`PageTemplate`, `PageTemplateFile`, `PageTextTemplate`, `PageTextTemplateFile`, `Macros`,
# `BOOLEAN_HTML_ATTRIBUTES`), modified: a template compiles to a native program instead of a
# Python module (`cook`), `render` calls it directly unless a `_render` function was set on
# the instance, `cook_check` is thread-safe, and `source` is a dump of the program (D11).
"""The template classes (D7): ``PageTemplate(body)``, ``PageTextTemplate(body)``, the
file-based ``PageTemplateFile(filename)`` / ``PageTextTemplateFile(filename)``, and their
bases ``BaseTemplate`` and ``BaseTemplateFile``, with Chameleon's configuration surface.

Configuration is passed as keyword arguments (they become instance attributes) or set as
class attributes on a subclass, as in Chameleon: ``auto_reload``, ``encoding``,
``boolean_attributes``, ``translate``, ``implicit_i18n_translate``,
``implicit_i18n_attributes``, ``on_error_handler``, ``strict``, ``trim_attribute_space``,
``restricted_namespace``, ``enable_data_attributes``, ``enable_comment_interpolation``,
``default_marker``, ``value_repr``, ``tokenizer``, ``debug``, ``keep_source``, ``keep_body``,
``extra_builtins``, ``expression_types``, ``default_expression``; for file templates also
``search_path``, ``package_name``, ``loader_class`` and ``prepend_relative_search_path``.
"""

from __future__ import annotations

import asyncio
import builtins as _builtins_module
import datetime
import hashlib
import importlib
import logging
import os
import pathlib
import re
import tempfile
import threading
import types
from collections.abc import Callable, Collection, Iterable, Mapping
from functools import partial
from typing import Any, ClassVar

from chameleon_rs._native import (
    DEFAULT_MARKER,
    Macro,
    Program,
    RenderAwaitable,
    Scope,
    render_page,
    render_page_async,
)
from chameleon_rs.astutil import Symbol
from chameleon_rs.codegen import reverse_builtin_map
from chameleon_rs.config import AUTO_RELOAD, CACHE_DIRECTORY, DEBUG_MODE, EAGER_PARSING
from chameleon_rs.exc import ExceptionFormatter, RenderError, TemplateError, UnsupportedError
from chameleon_rs.i18n import simple_translate
from chameleon_rs.loader import MemoryLoader, ModuleLoader, TemplateLoader, import_package_resource
from chameleon_rs.partials import PartialRenderer
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
from chameleon_rs.tokenize import Token
from chameleon_rs.utils import (
    DebuggingOutputStream,
    create_formatted_exception,
    detect_encoding,
    join,
    read_bytes,
    read_xml_encoding,
    value_repr,
)

log = logging.getLogger('chameleon.template')

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


def safe_get_package_version(name: str) -> str | None:
    import importlib.metadata  # imported when used: ~6 ms at startup

    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def get_package_versions() -> list[tuple[str, str]]:
    import importlib.metadata  # imported when used

    distributions = importlib.metadata.packages_distributions().values()
    versions = {x: safe_get_package_version(x) or '' for x in sum(distributions, [])}  # noqa: RUF017 - as in Chameleon
    return sorted(versions.items())


_pkg_digest: Any = None


def get_pkg_digest() -> Any:
    global _pkg_digest
    if _pkg_digest is None:
        _pkg_digest = hashlib.sha1(__name__.encode('utf-8'))
        for name, version in get_package_versions():
            _pkg_digest.update(name.encode('utf-8'))
            _pkg_digest.update(version.encode('utf-8'))
    return _pkg_digest.copy()


def _make_module_loader() -> ModuleLoader:
    remove = False
    if CACHE_DIRECTORY:
        path = CACHE_DIRECTORY
    else:
        path = tempfile.mkdtemp()
        remove = True

    return ModuleLoader(path, remove)


class BaseTemplate:
    """Template base class.

    Takes a string input which must be one of the following:

    - a string;
    - a utf-8 encoded byte string;
    - a byte string for an XML document that defines an encoding in the document preamble;
    - an HTML document that specifies the encoding via the META tag.

    Note that the template input is decoded, parsed and compiled on initialization.
    """

    default_encoding = 'utf-8'
    default_content_type: str | None = None

    # Strictly informational: used in exception formatting.
    filename: Any = '<string>'

    _cooked = False

    # Chameleon compiles templates into Python modules through its `loader`; chameleon-rs
    # compiles them natively and keeps the attribute for code that inspects it.
    loader: ModuleLoader | MemoryLoader
    if DEBUG_MODE or CACHE_DIRECTORY:
        loader = _make_module_loader()
    else:
        loader = MemoryLoader()

    output_stream_factory: type[list[str]]
    if DEBUG_MODE:
        output_stream_factory = DebuggingOutputStream
    else:
        output_stream_factory = list

    debug = DEBUG_MODE

    # Symbols available to every expression of the template (a render keyword of the same
    # name overrides them); updated with ``extra_builtins`` when the template compiles.
    builtins: ClassVar[dict[str, Any]] = {}
    extra_builtins: ClassVar[dict[str, Any]] = {}

    engine: Any = None

    # When ``strict`` is set, expressions must be valid at compile time. When not set, this
    # is only required at evaluation time.
    strict = True

    # How values are shown in a formatted render error.
    value_repr: staticmethod[[object], object] = staticmethod(value_repr)

    source: str | None

    def __init__(self, body: str | bytes | None = None, **config: Any) -> None:
        self.__dict__.update(config)

        if body is not None:
            self.write(body)

        # This is only necessary if the ``debug`` flag was passed as a keyword argument
        if self.__dict__.get('debug') is True:
            self.loader = _make_module_loader()

    def __call__(self, **kwargs: Any) -> Any:
        # A method of its own, as in Chameleon: the interpreter's stack depth at the render
        # matters for where deep macro recursion fails (`Errors::frame_budget`).
        return self.render(**kwargs)

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} {self.filename}>'

    @property
    def keep_body(self) -> bool:
        # By default, we only save the template body if we're in debugging mode.
        return self.__dict__.get('keep_body', DEBUG_MODE)  # type: ignore[no-any-return]

    @property
    def keep_source(self) -> bool:
        # By default, we only save the program dump if we're in debugging mode.
        return self.__dict__.get('keep_source', DEBUG_MODE)  # type: ignore[no-any-return]

    def cook(self, body: str) -> None:
        """Compiles ``body`` (decoded already)."""
        builtins_dict = self.builtins.copy()
        builtins_dict.update(self.extra_builtins)
        try:
            program = self._compile_program(body, builtins_dict)
        except TemplateError as exc:
            # normalize to str
            exc.token.filename = str(self.filename)
            raise
        # One attribute read by renders and macro lookups, so a reload in another thread can
        # never pair one compilation's program with another's builtins.
        self._compiled = (program, builtins_dict)
        self._builtin_dict = builtins_dict
        self._program = program
        self._cooked = True

        if self.keep_body:
            self.body = body
        if self.keep_source:
            # D11: a readable dump of the compiled program, not generated Python code.
            self.source = program.dump()

    def _compile_program(self, body: str, builtins: Mapping[str, Any]) -> Program:
        self.parse(body)
        raise NotImplementedError('Must be implemented by subclass.')

    def cook_check(self) -> bool | None:
        assert self._cooked
        return None

    def parse(self, body: str) -> Any:
        raise NotImplementedError('Must be implemented by subclass.')

    def _render(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError('Must be implemented by subclass.')

    def render(self, **__kw: Any) -> str:
        """Chameleon's render: the template's ``_render(stream, econtext, rcontext, ...)``
        function writes to a stream, and an error is re-raised formatted. ``PageTemplate``
        renders natively instead, unless an instance has a ``_render`` function of its own."""
        econtext = Scope(__kw)
        rcontext: dict[str, Any] = {}
        self.cook_check()
        stream = self.output_stream_factory()
        target_language = __kw.get('target_language')
        try:
            self._render(stream, econtext, rcontext, target_language=target_language)
        except RecursionError:
            raise
        except BaseException as caught:
            errors = rcontext.get('__error__')
            if not errors:
                raise
            formatter = caught.__str__
            if isinstance(formatter, ExceptionFormatter):
                if errors is not formatter._errors:
                    formatter._errors.extend(errors)
                raise
            formatter = ExceptionFormatter(errors, econtext, rcontext, self.value_repr)
            exc = caught
            try:
                exc = create_formatted_exception(caught, type(caught), formatter, RenderError)
            except TypeError:
                pass
            # Raised inside the `except` block, with the original traceback, as Chameleon does.
            raise exc.with_traceback(caught.__traceback__)

        return join(stream)

    async def render_async(self, **__kw: Any) -> str:
        """``render`` as a coroutine. A template with a render function of its own (Chameleon's
        protocol) renders synchronously; ``PageTemplate`` suspends on awaitable values."""
        return self.render(**__kw)

    def write(self, body: str | bytes) -> None:
        encoding: str | None
        if isinstance(body, bytes):
            body, encoding, content_type = read_bytes(body, self.default_encoding)
        elif body.startswith('<?xml'):
            content_type = 'text/xml'
            encoding = read_xml_encoding(body.encode('utf-8'))
        else:
            content_type, encoding = detect_encoding(body, self.default_encoding)

        self.content_type = content_type or self.default_content_type
        self.content_encoding = encoding

        self.cook(body)

    def digest(self, body: str, names: Collection[str]) -> str:
        class_name = type(self).__name__.encode('utf-8')
        sha = get_pkg_digest()
        sha.update(body.encode('utf-8', 'ignore'))
        sha.update(class_name)
        digest: str = sha.hexdigest()

        filename = str(self.filename)
        if filename and filename != BaseTemplate.filename:
            digest = os.path.splitext(filename)[0] + '-' + digest

        return digest


class BaseTemplateFile(BaseTemplate):
    """File-based template base class: compiled on first use, and again when the file
    changed if ``auto_reload`` is set.

    Relative path names are supported only when a template loader is provided as the
    ``loader`` parameter.
    """

    # Auto reload is not enabled by default because it's a significant performance hit
    auto_reload = AUTO_RELOAD

    # Guards compiling; an instance gets its own in `__init__`.
    _lock = threading.RLock()

    _v_last_read: float | None = None

    def __init__(
        self,
        filename: str | os.PathLike[str],
        auto_reload: bool | None = None,
        package_name: str | None = None,
        post_init_hook: Callable[[], object] | None = None,
        **config: Any,
    ) -> None:
        self._lock = threading.RLock()
        if package_name is None:
            # Normalize filename
            filename = os.path.abspath(os.path.normpath(os.path.expanduser(filename)))
        self.package_name = package_name
        self.filename = filename

        # Override reload setting only if value is provided explicitly
        if auto_reload is not None:
            self.auto_reload = auto_reload

        super().__init__(**config)

        if post_init_hook is not None:
            post_init_hook()

        if EAGER_PARSING:
            self.cook_check()

    def cook_check(self) -> bool:
        """Compiles the template if it is not compiled yet, or (with ``auto_reload``) if its
        file changed since it was read; returns whether it compiled. Thread-safe: one thread
        compiles while the others wait, and a render never sees a half-updated template.

        The lock is per template: a ``cook`` override that compiles another template (for
        example by looking up its macros) can deadlock with a thread doing the reverse."""
        if self.auto_reload:
            mtime = self.mtime()
            if mtime != self._v_last_read:
                with self._lock:
                    # Again under the lock: a thread that read the time before another
                    # reloaded the file must not set it back to the older one.
                    mtime = self.mtime()
                    if mtime != self._v_last_read:
                        self._v_last_read = mtime
                        self._cooked = False
                        return self._cook_locked()

        if self._cooked is False:
            with self._lock:
                return self._cook_locked()

        return False

    def _cook_locked(self) -> bool:
        if self._cooked is not False:
            return False
        body = self.read()
        log.debug('cooking %r (%d bytes)...', self.filename, len(body))
        self.cook(body)
        return True

    def mtime(self) -> float:
        filename = self.filename
        if self.package_name is not None:
            with import_package_resource(self.package_name) as path:
                joined = path.joinpath(filename)
                if isinstance(joined, pathlib.Path):
                    return joined.stat().st_mtime

                # Assume this is a zip-like path object (a `zipfile.Path`: `at` and `root` are
                # not part of the `Traversable` protocol):
                filename = joined.at  # ty: ignore[unresolved-attribute]
                timetuple = path.root.getinfo(filename).date_time  # ty: ignore[unresolved-attribute]
                return datetime.datetime(*timetuple).timestamp()
        try:
            return os.path.getmtime(filename)
        except OSError:
            return 0

    def read(self) -> str:
        if self.package_name is not None:
            with import_package_resource(self.package_name) as files:
                path = files.joinpath(self.filename)
                data = path.read_bytes()
        else:
            with open(self.filename, 'rb') as f:
                data = f.read()

        body, encoding, content_type = read_bytes(data, self.default_encoding)

        self.content_type = content_type or self.default_content_type
        self.content_encoding = encoding

        return body

    def _get_filename(self) -> Any:
        return self.__dict__.get('filename')

    def _set_filename(self, filename: Any) -> None:
        self.__dict__['filename'] = filename
        self._v_last_read = None
        self._cooked = False

    filename = property(_get_filename, _set_filename)  # type: ignore[assignment]


class Macros:
    """The ``macros`` template builtin: ``macros['name']`` is the macro defined with
    ``metal:define-macro="name"`` (a ``KeyError`` if there is none)."""

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
        program, builtins = template._compiled
        cached = self._cache.get(name)
        if cached is not None and cached[0] is program:
            return cached[1]
        macro = Macro(program, name, builtins)
        self._cache[name] = (program, macro)
        return macro

    @property
    def names(self) -> list[str]:
        self.template.cook_check()
        return list(self.template._program.macro_names)


def _marker_value(node: Any) -> Any:
    """The object a ``default_marker`` setting stands for: Chameleon takes an AST node
    (``Symbol(obj)`` by default). A ``Symbol``'s object must be importable, as Chameleon's
    generated module imports it: ``TypeError`` for an object without a ``__name__``,
    ``ImportError`` if its module does not have it."""
    kind = type(node).__name__
    if kind == 'Symbol':
        return _importable(node.value)
    if kind == 'Builtin':
        return getattr(_builtins_module, node.id)
    if kind == 'Constant':
        return node.value
    return node


def _importable(value: Any) -> Any:
    if value is DEFAULT_MARKER or isinstance(value, types.ModuleType):
        return value
    if not hasattr(value, '__name__'):
        raise TypeError(value)
    if reverse_builtin_map.get(value) is not None:
        return value
    module = importlib.import_module(value.__module__)
    try:
        return getattr(module, value.__name__)
    except AttributeError:
        msg = f'cannot import name {value.__name__!r} from {value.__module__!r}'
        raise ImportError(msg, name=value.__module__) from None


class PageTemplate(BaseTemplate):
    """Constructor for the page template language, rendered by the Rust VM.

    Takes a string input as the only positional argument::

      template = PageTemplate("<div>Hello, ${name}.</div>")

    Output is of type ``str``. See the module docs for the configuration keywords.

    Every template has the builtin ``render_partial(path, **kwargs)`` (D10, see
    ``chameleon_rs.partials``), which a render keyword or ``extra_builtins`` entry of the same
    name overrides.
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
    default_content_type = 'text/html'

    translate: Any = staticmethod(simple_translate)

    encoding: str | None = None

    boolean_attributes: Collection[str] | None = None

    mode = 'xml'

    implicit_i18n_translate = False

    implicit_i18n_attributes: Collection[str] = frozenset()

    trim_attribute_space = False

    enable_data_attributes = False

    enable_comment_interpolation = True

    on_error_handler: Callable[[BaseException], object] | None = None

    restricted_namespace = True

    tokenizer: Callable[..., Iterable[str]] | None = None

    default_marker: Any = Symbol(DEFAULT_MARKER)

    # chameleon-rs (opt-in, D9): in ``render_async``, native partials called with an awaitable
    # argument render concurrently, each in a task of its own.
    concurrent = False

    _defaults_key: tuple[Any, ...] | None = None
    _encoded: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    # `body` is any type: in PageTemplateFile's MRO this passes the filename on.
    def __init__(self, body: Any, **config: Any) -> None:
        self.macros = Macros(self)
        self._partial_renderer = PartialRenderer(self)
        super().__init__(body, **config)

    def __getitem__(self, name: str) -> Macro:
        return self.macros[name]

    # Read-only here, a writable class attribute in BaseTemplate: as in Chameleon.
    @property
    def builtins(self) -> dict[str, Any]:  # ty: ignore[invalid-property-type-override]
        """The template builtins: names every expression can use (a render keyword of the
        same name overrides them)."""
        return self._builtins()

    def _builtins(self) -> dict[str, Any]:
        return {'template': self, 'macros': self.macros, 'nothing': None, 'render_partial': self._partial_renderer}

    def parse(self, body: str) -> Any:
        raise NotImplementedError('chameleon-rs compiles templates natively; there is no program tree in Python.')

    def _compile_program(self, body: str, builtins: Mapping[str, Any]) -> Program:
        """`PageTemplate.parse` and the compiler: HTML boolean attributes and newline
        normalization unless the content type is XML."""
        boolean_attributes = self.boolean_attributes
        if self.content_type != 'text/xml':
            if boolean_attributes is None:
                boolean_attributes = BOOLEAN_HTML_ATTRIBUTES
            body = body.replace('\r\n', '\n').replace('\r', '\n')
        token_lengths = None
        tokenizer = self.tokenizer
        if tokenizer is not None:
            # The parser takes the tokens as consecutive slices of one source: the tokens
            # joined (the body itself when they cover it, as Chameleon's tokenizers do).
            tokens = list(tokenizer(body, self.filename))
            body = ''.join(tokens)
            token_lengths = [len(t) for t in tokens]
        marker = _marker_value(self.default_marker)
        options = {name: getattr(self, name) for name in PROGRAM_OPTIONS}
        body, surrogate_base = _map_surrogates(body)
        if surrogate_base is not None:
            options['surrogate_base'] = surrogate_base
        try:
            return Program(
                body,
                str(self.filename),
                expression_types=native_types(self.expression_types),
                default_expression=self.default_expression,
                builtins=sorted(builtins),
                fast_path=self.__dict__.get('_fast_path', True),
                default_marker=None if marker is DEFAULT_MARKER else marker,
                token_lengths=token_lengths,
                mode=self.mode,
                boolean_attributes=boolean_attributes or (),
                **options,
            )
        except UnicodeEncodeError as exc:
            if exc.object is not body:
                raise
            raise _surrogate_error(body, exc.start) from None

    @property
    def include(self) -> Macro:
        """The whole template as a macro (``metal:use-macro="template"``): Chameleon's
        ``include(stream, econtext, rcontext, ...)``, which renders the main function into
        ``stream``."""
        self.cook_check()
        program, builtins = self._compiled
        return Macro(program, None, builtins)

    def _render(self, *args: Any, **kwargs: Any) -> None:
        # Chameleon's compiled render function: `(stream, econtext, rcontext, domain=None,
        # context=None, target_language=None)`.
        self.include(*args, **kwargs)

    def _render_defaults(self, encoding: str | None, translate: Callable[..., Any] | None) -> dict[str, Any]:
        if translate is None:
            translate = self.translate

            # This should not be necessary, but Chameleon includes it for backward
            # compatibility.
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
                return txl(msgid, **kwargs)  # type: ignore[no-any-return]

            if txl is simple_translate:
                # For a value that is not bytes this is `simple_translate` itself: the native
                # conversion takes its fast path (chameleon_partials and the integrations
                # render with `encoding='utf-8'`).
                translate.__chameleon_rs_simple__ = True  # ty: ignore[unresolved-attribute] - a function attribute

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
        """Render template to string.

        If provided, the ``encoding`` argument overrides the template default value.
        Additional keyword arguments are passed as template variables; ``translate``
        overrides the template's translation function and ``target_language`` is the
        language passed to it when no ``i18n:target`` applies.
        """
        # Natively (`chameleon_rs._native.render_page`, Phase 10), in this order: the defaults
        # (`_defaults` while `translate`, `encoding` and `on_error_handler` are unchanged and
        # neither argument is given, else `_encoded_defaults`), `cook_check()` (compiling may
        # set a render function: pyramid_chameleon's `macro=` does, in `cook`), then
        # `_render_protocol` if the instance has a `_render`, else the program renders with
        # the keywords, the defaults they lack and a fresh `repeat`.
        return render_page(self, encoding, kw)  # type: ignore[no-any-return]

    async def render_async(self, encoding: str | None = None, *, offload: bool = False, **kw: Any) -> str:
        """Render template to string, awaiting awaitable values (D9).

        The same as ``render``, except that when an expression's final value is awaitable (a
        coroutine, a task, a future, anything ``await`` accepts), the render suspends until the
        value is awaited and continues with its result. Values inside an expression are not
        awaited (``coro().title`` is the coroutine's attribute), and a condition is tested on
        the awaited value. An error raised by an awaited value is raised where the expression
        is, so ``tal:on-error`` handles it; cancellation passes through.

        Opt-in concurrency: calls of ``@chameleon_rs.concurrent`` helpers written as a whole
        ``${...}`` are awaited concurrently, and with the template setting ``concurrent=True``
        native partials called with an awaitable argument render concurrently (see
        ``chameleon_rs._async``).

        Experimental, opt-in: ``offload=True`` runs ``render`` (synchronous: awaitable values
        are not awaited) in the event loop's default executor, a worker thread, and awaits it.
        On a free-threaded build renders then run in parallel with the event loop and each
        other; on a GIL build it helps only renders dominated by long text, whose escaping
        releases the GIL (docs/plan.md, Phase 11 results). ``offload`` is therefore not a
        template variable here.
        """
        return await _render_async(self, encoding, kw, self.concurrent, offload)

    def _encoded_defaults(self, encoding: str | None, translate: Callable[..., Any] | None) -> dict[str, Any]:
        """The defaults for an `encoding` or `translate` argument, kept while the arguments and
        the settings they depend on are the same (chameleon_partials passes `encoding`)."""
        key = (encoding, translate, self.translate, self.encoding, self.on_error_handler)
        cached = self._encoded
        # By identity: equal but distinct translators (one per language) are different.
        if cached is not None and len(cached[0]) == len(key) and all(a is b for a, b in zip(cached[0], key)):
            return cached[1]
        defaults = self._render_defaults(encoding, translate)
        self._encoded = (key, defaults)
        return defaults

    def _render_protocol(self, kw: dict[str, Any], defaults: Mapping[str, Any]) -> str:
        # A render function set on the instance: Chameleon's protocol.
        for name, value in defaults.items():
            kw.setdefault(name, value)
        if 'repeat' not in kw:
            kw['repeat'] = RepeatDict({})
        return BaseTemplate.render(self, **kw)

    def digest(self, body: str, names: Collection[str]) -> str:
        hex_digest = super().digest(body, names)
        digest = hashlib.sha256(hex_digest.encode('utf-8'))
        digest.update(';'.join(names).encode('utf-8'))

        for attr in ('trim_attribute_space', 'implicit_i18n_translate', 'strict'):
            v = getattr(self, attr)
            digest.update((f';{attr}={v}').encode('ascii'))

        return digest.hexdigest()[:32]


async def _render_async(
    template: PageTemplate, encoding: str | None, kw: dict[str, Any], concurrent: bool, offload: bool = False
) -> str:
    """``PageTemplate.render_async`` with ``concurrent`` and ``offload`` given (the
    integrations' per-view settings)."""
    if type(template).render not in _BASE_RENDERS:
        # A subclass's own `render` (adding defaults, post-processing): honoured, as
        # synchronous (awaitable values are not awaited).
        return await _overridden(template, encoding, kw, offload)  # type: ignore[no-any-return]
    if offload:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(PageTemplate.render, template, encoding, **kw))  # type: ignore[no-any-return]
    result = render_page_async(template, encoding, kw, bool(concurrent))
    if isinstance(result, RenderAwaitable):
        return await result  # type: ignore[no-any-return]
    return result  # type: ignore[no-any-return]


async def _overridden(template: PageTemplate, encoding: str | None, kw: dict[str, Any], offload: bool) -> Any:
    """The template's own `render` (a subclass's), in a worker thread with `offload`."""
    call = partial(template.render, **kw) if encoding is None else partial(template.render, encoding, **kw)
    if offload:
        return await asyncio.get_running_loop().run_in_executor(None, call)
    return call()


_LONE_SURROGATE = re.compile('[\ud800-\udfff]')
_PRIVATE_USE = re.compile('[\ue000-\uf8ff]')
# Numeric character references as Chameleon decodes them (`&#` and `\d{1,5}`, or `&#x` and
# `\w{1,8}`, then `int()`, which takes any Unicode digit and underscores between digits).
_NUMERIC_REFERENCE = re.compile(r'&#(x?)(\w{1,8});')
_SURROGATE_TABLES: dict[int, dict[int, int]] = {}


def _reference_codes(body: str) -> list[int]:
    codes = []
    if '&#' in body:
        for x, digits in _NUMERIC_REFERENCE.findall(body):
            try:
                codes.append(int(digits, 16) if x else int(digits))
            except ValueError:
                continue
    return codes


def _map_surrogates(body: str) -> tuple[str, int | None]:
    """A source's lone surrogates, which the Rust core's UTF-8 text cannot hold, mapped one
    to one onto 2,048 code points of the Private Use Area (U+E000-U+F8FF) that the source
    (its characters and its numeric character references) does not use; the native program
    maps them back wherever source text leaves it, and decodes character references to lone
    surrogates into the same block (architecture.md, Phase 8 amendments). Returns the body and
    the block's first code point, or the body unchanged and ``None`` when there is nothing to
    map (or no free block: compiling then refuses it)."""
    codes = _reference_codes(body)
    if _LONE_SURROGATE.search(body) is None and not any(0xD800 <= c < 0xE000 for c in codes):
        return body, None
    used_codes = {ord(c) for c in _PRIVATE_USE.findall(body)}
    used_codes.update(c for c in codes if 0xE000 <= c < 0xF900)
    start = 0xE000
    base = None
    for used in sorted(used_codes) + [0xF900]:
        if used - start >= 0x800:
            base = start
            break
        start = used + 1
    if base is None:
        return body, None
    table = _SURROGATE_TABLES.get(base)
    if table is None:
        table = {code: base + code - 0xD800 for code in range(0xD800, 0xE000)}
        if len(_SURROGATE_TABLES) < 64:
            _SURROGATE_TABLES[base] = table
    return body.translate(table), base


def _surrogate_error(body: str, at: int) -> UnsupportedError:
    """A template source with lone surrogates and characters in every private-use block
    they could be mapped to (architecture.md, Phase 8 amendments) is refused at compile
    time."""
    code = ord(body[at])
    return UnsupportedError(
        f'A lone surrogate (U+{code:04X}) in a template source whose private-use characters leave no 2,048 '
        'code points of U+E000-U+F8FF free',
        Token(body[at], at, body),
    )


class PageTemplateFile(PageTemplate, BaseTemplateFile):
    """File-based constructor: the template is read from ``filename`` (or, with
    ``package_name``, from that package's resources) when first used.

    File templates have the ``load:`` expression type, which loads templates relative to
    this one: ``search_path`` (a path or a sequence of paths; with
    ``prepend_relative_search_path``, the default, the template's own directory comes first)
    and ``loader_class`` (``TemplateLoader``), which gets the same configuration keywords.
    """

    expression_types: ClassVar[dict[str, Any]] = {
        **PageTemplate.expression_types,
        'load': partial(ProxyExpr, '__loader', ignore_prefix=False),
    }

    prepend_relative_search_path = True

    # The relative template loader: `loader_class(...).bind(type(self))`, set by `__init__`.
    _loader: Callable[[str], Any]

    def __init__(
        self,
        filename: str | os.PathLike[str],
        loader_class: type[TemplateLoader] = TemplateLoader,
        package_name: str | None = None,
        search_path: Iterable[str] | str | None = None,
        **config: Any,
    ) -> None:
        if search_path is None:
            paths: list[str] = []
        elif isinstance(search_path, str):
            paths = [search_path]
        else:
            paths = list(search_path)

        # For `render_partial`: partials are looked for on this search path (and siblings are
        # built with these settings), before this template's own folder.
        self._partial_paths = list(paths)
        self._partial_config = dict(config)

        def post_init() -> None:
            # If the flag is set (this is the default), prepend the path relative to the
            # template file to the search path
            if self.prepend_relative_search_path:
                path: Any
                if isinstance(self.filename, pathlib.PurePath) and not isinstance(self.filename, str):
                    path = self.filename.parent
                else:
                    path = os.path.dirname(self.filename)
                    if package_name is not None:
                        path = f'{package_name}:{path}'
                paths.insert(0, path)

            loader = loader_class(search_path=paths, **config)

            # Bind relative template loader instance to the same template class, providing
            # the same keyword arguments.
            self._loader = loader.bind(type(self))

        super().__init__(
            filename,
            package_name=package_name,
            post_init_hook=post_init,
            **config,
        )

    def _builtins(self) -> dict[str, Any]:
        d = super()._builtins()
        d['__loader'] = self._loader
        return d


class PageTextTemplate(PageTemplate):
    """Text-based template class: only ``${...}`` interpolation, no markup, nothing escaped.

    Takes a non-XML input::

      template = PageTextTemplate("Hello, ${name}.")
    """

    mode = 'text'


class PageTextTemplateFile(PageTemplateFile):
    """File-based text template. Rendering returns ``bytes``, encoded with the template's
    ``encoding`` (UTF-8 by default), as Chameleon's does."""

    mode = 'text'

    # `bytes` where the base class returns `str`, as Chameleon's PageTextTemplateFile does.
    def render(self, encoding: str | None = None, **kw: Any) -> bytes:  # ty: ignore[invalid-method-override]
        result = super().render(encoding, **kw)
        return result.encode(self.encoding or 'utf-8')

    # `bytes`, as `render`.
    async def render_async(  # ty: ignore[invalid-method-override]
        self, encoding: str | None = None, *, offload: bool = False, **kw: Any
    ) -> bytes:
        if type(self).render is not PageTextTemplateFile.render:
            return await _overridden(self, encoding, kw, offload)  # type: ignore[no-any-return]
        result = await super().render_async(encoding, offload=offload, **kw)
        return result.encode(self.encoding or 'utf-8')


# The `render` methods `render_async` stands in for; a subclass's own is called instead.
_BASE_RENDERS = (PageTemplate.render, PageTextTemplateFile.render)


__all__ = [
    'BOOLEAN_HTML_ATTRIBUTES',
    'BaseTemplate',
    'BaseTemplateFile',
    'Macro',
    'Macros',
    'PageTemplate',
    'PageTemplateFile',
    'PageTextTemplate',
    'PageTextTemplateFile',
]
