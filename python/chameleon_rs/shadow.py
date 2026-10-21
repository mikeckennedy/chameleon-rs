"""Shadow mode (plan.md, Phase 8): render every template with both engines and report any
difference, for staging an application on real traffic before switching it over.

Opt-in and only for staging: nothing here runs unless :func:`install` is called, and then
every sampled render happens twice. It needs stock Chameleon importable as ``chameleon``
(so not together with :func:`chameleon_rs.compat.install`): the application keeps using
stock Chameleon, and each outermost ``render`` of a stock page template is repeated with a
chameleon-rs twin of that template (same file or source, same settings) and the same
arguments::

    import chameleon_rs.shadow

    chameleon_rs.shadow.install()                  # serve Chameleon's output, check ours
    chameleon_rs.shadow.install(serve='rust')      # serve ours, check against Chameleon's

Templates rendered while the twin renders (``chameleon_partials.render_partial`` calls a stock
template's ``render``) are rendered by their own twins, and stock templates passed as render
arguments (a layout whose macros the page uses) are replaced by their twins, so the twin's
output is chameleon-rs's throughout. A difference in the output, or in the exception raised
(its type and message, object addresses aside), is passed to ``report`` as a
:class:`ShadowDifference`; by default it is logged as a warning on the ``chameleon_rs.shadow``
logger with a short diff.

Costs and limits:

- Each sampled render happens twice, in the same thread: the request's render latency doubles.
  Under an asyncio server with one thread per worker (Granian ST, Uvicorn) the event loop is
  blocked for both renders, so keep ``sample_rate`` well below 1 (0.01-0.1) on real traffic.
- The arguments are used twice, so an iterator argument is exhausted by the first render and
  callables run twice: shadow side-effect-free models only.
- :class:`ShadowDifference` holds both engines' full output, which may contain personal data;
  the default logger writes only a capped diff (lines cut at 200 characters, at most 4 KB) and
  each output's length and SHA-256, once per distinct difference per ``log_interval``
  seconds, with a count of the repeats it suppressed.
- A failure inside the shadow (the other engine raising anything, even a Rust panic, or the
  report itself failing) is logged and never reaches the request; ``KeyboardInterrupt`` and
  ``SystemExit`` propagate.
- To build twins of string templates, the source of every template cooked while shadow mode
  is on is kept for as long as the template lives. Templates cooked before :func:`install` are
  not shadowed. Macros of stock templates reached other than through a render argument
  (``template.macros`` of a stock object built inside the model) are rendered by stock
  Chameleon within the twin.
"""

from __future__ import annotations

import contextvars
import dataclasses
import difflib
import hashlib
import logging
import random
import re
import sys
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

__all__ = ['ShadowDifference', 'install', 'installed', 'uninstall']

log = logging.getLogger('chameleon_rs.shadow')

# Chameleon's template keywords that a twin is built with, read from the stock template.
TWIN_SETTINGS = (
    'encoding',
    'auto_reload',
    'translate',
    'boolean_attributes',
    'implicit_i18n_translate',
    'implicit_i18n_attributes',
    'on_error_handler',
    'strict',
    'trim_attribute_space',
    'restricted_namespace',
    'enable_data_attributes',
    'enable_comment_interpolation',
    'value_repr',
    'extra_builtins',
    'search_path',
    'prepend_relative_search_path',
    'default_extension',
    'tokenizer',
)

_ADDRESS = re.compile(r'0x[0-9a-fA-F]+')
_DIFF_LINE = 200
_DIFF_BYTES = 4096
_FATAL = (KeyboardInterrupt, SystemExit)


@dataclasses.dataclass(frozen=True)
class ShadowDifference:
    """One render whose outcome differs between the engines."""

    filename: str
    served: str
    """The engine whose outcome the application received (``'chameleon'`` or ``'rust'``)."""
    chameleon: str
    """Chameleon's full output, or its exception as ``Type: message`` (may hold personal data)."""
    rust: str
    """chameleon-rs's full output, or its exception (``Type: message``)."""
    diff: str
    """A unified diff of the two (lines split after each ``>`` and cut at 200 characters), at
    most 4 KB."""


class _Installed:
    def __init__(self, serve: str, sample_rate: float, report: Callable[[ShadowDifference], None]) -> None:
        import chameleon
        import chameleon.zpt.template as stock

        if 'chameleon_rs' in (getattr(chameleon, '__file__', None) or ''):
            raise RuntimeError('shadow mode needs stock Chameleon as `chameleon`, not chameleon_rs.compat')
        self.serve = serve
        self.sample_rate = sample_rate
        self.report = report
        self.random = random.Random()
        self.stock = stock
        self.twins: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
        self.lock = threading.Lock()
        # The source of string templates, which Chameleon does not keep (`keep_body`).
        self.bodies: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
        self.originals = {cls: cls.__dict__['render'] for cls in (stock.PageTemplate, stock.PageTextTemplateFile)}
        for cls in self.originals:
            cls.render = self.wrap(cls)
        base = sys.modules['chameleon.template'].BaseTemplate
        self.cook = base.__dict__['cook']
        bodies, cook = self.bodies, self.cook

        def remember_body(template: Any, body: str) -> None:
            cook(template, body)
            bodies[template] = body

        remember_body.__wrapped__ = cook  # type: ignore[attr-defined]
        base.cook = remember_body
        self.base = base

    def remove(self) -> None:
        for cls, render in self.originals.items():
            cls.render = render
        self.base.cook = self.cook

    def twin(self, template: Any) -> Any:
        twin = self.twins.get(template)
        if twin is not None:
            return twin
        import chameleon_rs

        settings = {name: template.__dict__[name] for name in TWIN_SETTINGS if name in template.__dict__}
        stock = self.stock
        text = isinstance(template, (stock.PageTextTemplate, stock.PageTextTemplateFile))
        if isinstance(template, stock.PageTemplateFile):
            cls = chameleon_rs.PageTextTemplateFile if text else chameleon_rs.PageTemplateFile
            twin = cls(template.filename, **settings)
        else:
            body = self.bodies.get(template)
            if body is None:
                return None  # cooked before shadow mode started: not shadowed
            cls = chameleon_rs.PageTextTemplate if text else chameleon_rs.PageTemplate
            twin = cls(body, filename=template.filename, **settings)
        with self.lock:
            return self.twins.setdefault(template, twin)

    def twin_arguments(self, kw: dict[str, Any]) -> dict[str, Any]:
        base = self.stock.PageTemplate
        return {k: (self.twin(v) or v) if isinstance(v, base) else v for k, v in kw.items()}

    def wrap(self, cls: type) -> Callable[..., Any]:
        original = self.originals[cls]
        shadow = self

        def render(self: Any, *args: Any, **kw: Any) -> Any:
            state = _STATE.get()
            if state == 'rust':
                # A stock template rendered while a twin renders (a partial): its twin.
                twin = shadow.twin(self)
                if twin is not None:
                    return twin.render(*args, **shadow.twin_arguments(kw))
                return original(self, *args, **kw)
            if state == 'chameleon' or shadow.random.random() >= shadow.sample_rate:
                return original(self, *args, **kw)
            try:
                twin = shadow.twin(self)
            except _FATAL:
                raise
            except BaseException as e:  # noqa: BLE001 - reported; Chameleon serves the template
                shadow.report_difference(self, {'chameleon': (True, ''), 'rust': (False, e)}, served='chameleon')
                return original(self, *args, **kw)
            if twin is None:
                return original(self, *args, **kw)
            return shadow.both(self, twin, original, args, kw)

        render.__wrapped__ = original  # type: ignore[attr-defined]
        return render

    def both(
        self, template: Any, twin: Any, original: Callable[..., Any], args: tuple[Any, ...], kw: dict[str, Any]
    ) -> Any:
        def run(engine: str) -> tuple[bool, Any]:
            token = _STATE.set(engine)
            try:
                if engine == 'chameleon':
                    return True, original(template, *args, **kw)
                return True, twin.render(*args, **self.twin_arguments(kw))
            except _FATAL:
                raise
            except BaseException as e:  # noqa: BLE001 - a Rust panic is a BaseException too
                return False, e
            finally:
                _STATE.reset(token)

        other = 'rust' if self.serve == 'chameleon' else 'chameleon'
        outcomes = {self.serve: run(self.serve)}
        try:
            outcomes[other] = run(other)
            if _summary(outcomes['chameleon']) != _summary(outcomes['rust']):
                self.report_difference(template, outcomes)
        except _FATAL:
            raise
        except BaseException:  # the shadow never breaks the request
            log.exception('chameleon_rs.shadow: comparing %s failed', getattr(template, 'filename', '<string>'))
        ok, value = outcomes[self.serve]
        if not ok:
            raise value
        return value

    def report_difference(
        self, template: Any, outcomes: dict[str, tuple[bool, Any]], served: str | None = None
    ) -> None:
        try:
            texts = {engine: _text(outcome) for engine, outcome in outcomes.items()}
            difference = ShadowDifference(
                filename=str(getattr(template, 'filename', '<string>')),
                served=served or self.serve,
                chameleon=texts['chameleon'],
                rust=texts['rust'],
                diff=_diff(texts['chameleon'], texts['rust']),
            )
            self.report(difference)
        except _FATAL:
            raise
        except BaseException:  # a report must never break the render
            log.exception('chameleon_rs.shadow: reporting a difference failed')


_STATE: contextvars.ContextVar[str | None] = contextvars.ContextVar('chameleon_rs_shadow', default=None)
_installed: _Installed | None = None
_install_lock = threading.Lock()


def _message(error: BaseException) -> str:
    try:
        return str(error)
    except BaseException as e:  # an exception whose str() fails
        if isinstance(e, _FATAL):
            raise
        return f'<str() failed: {type(e).__name__}>'


def _text(outcome: tuple[bool, Any]) -> str:
    ok, value = outcome
    if ok:
        return value.decode('utf-8', 'surrogatepass') if isinstance(value, bytes) else str(value)
    return f'{type(value).__name__}: {_message(value)}'


def _summary(outcome: tuple[bool, Any]) -> tuple[bool, Any]:
    ok, value = outcome
    if ok:
        return True, value
    message = _ADDRESS.sub('0x?', _message(value)).replace('chameleon_rs.', 'chameleon.')
    return False, (type(value).__name__, message)


def _diff(first: str, second: str) -> str:
    """A unified diff, lines split after each ``>`` and cut at 200 characters, at most 4 KB."""
    lines = [
        [
            line if len(line) <= _DIFF_LINE else line[:_DIFF_LINE] + '...\n'
            for line in t.replace('>', '>\n').splitlines(True)
        ]
        for t in (first, second)
    ]
    out: list[str] = []
    size = 0
    for line in difflib.unified_diff(*lines, 'chameleon', 'chameleon-rs', n=1):
        size += len(line)
        if size > _DIFF_BYTES:
            out.append('... (diff cut at 4 KB)\n')
            break
        out.append(line)
    return ''.join(out)


def _digest(text: str) -> str:
    data = text.encode('utf-8', 'surrogatepass')
    return f'{len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()[:16]}'


class _Logger:
    """The default report: a warning per distinct difference (file and diff) at most once per
    ``interval`` seconds, with the count of repeats in between."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.lock = threading.Lock()
        self.seen: dict[tuple[str, str], tuple[float, int]] = {}

    def __call__(self, difference: ShadowDifference) -> None:
        key = (difference.filename, hashlib.sha256(difference.diff.encode('utf-8', 'surrogatepass')).hexdigest())
        now = time.monotonic()
        with self.lock:
            last, suppressed = self.seen.get(key, (None, 0))
            if last is not None and now - last < self.interval:
                self.seen[key] = (last, suppressed + 1)
                return
            if len(self.seen) > 10_000:
                self.seen.clear()
            self.seen[key] = (now, 0)
        log.warning(
            'chameleon_rs.shadow: %s renders differently (serving %s; chameleon %s, chameleon-rs %s%s):\n%s',
            difference.filename,
            difference.served,
            _digest(difference.chameleon),
            _digest(difference.rust),
            f'; {suppressed} more since the last report' if suppressed else '',
            difference.diff,
        )


def install(
    *,
    serve: str = 'chameleon',
    sample_rate: float = 1.0,
    report: Callable[[ShadowDifference], None] | None = None,
    log_interval: float = 600.0,
) -> None:
    """Starts shadow mode: from now on, a ``sample_rate`` share of the outermost renders of
    stock Chameleon page templates also renders with chameleon-rs (doubling their latency:
    keep it well below 1 on real traffic), and the application gets the outcome (output or
    exception) of the ``serve`` engine. Differences go to ``report`` (default: a capped warning
    on the ``chameleon_rs.shadow`` logger, once per distinct difference per ``log_interval``
    seconds). Calling it again replaces the settings."""
    global _installed
    if serve not in ('chameleon', 'rust'):
        raise ValueError("serve must be 'chameleon' or 'rust'")
    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError('sample_rate must be between 0 and 1')
    with _install_lock:
        if _installed is not None:
            _installed.remove()
        _installed = _Installed(serve, sample_rate, report or _Logger(log_interval))


def uninstall() -> None:
    """Stops shadow mode, restoring stock Chameleon's ``render`` methods."""
    global _installed
    with _install_lock:
        if _installed is not None:
            _installed.remove()
            _installed = None


def installed() -> bool:
    return _installed is not None
