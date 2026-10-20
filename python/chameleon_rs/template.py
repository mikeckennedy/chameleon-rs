# Ported from Chameleon's src/chameleon/zpt/template.py (PageTemplate.render's setup of
# `__translate`, `__decode`, `__on_error_handler`, `target_language` and `repeat`),
# modified: the template compiles to a native program instead of a Python module.
"""The public template classes (D7). Only ``PageTemplate(body)`` exists so far."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from chameleon_rs._native import Program
from chameleon_rs.i18n import simple_translate
from chameleon_rs.tal import RepeatDict


class PageTemplate:
    """A page template compiled from a string, rendered by the Rust VM.

    ``partials`` is a temporary prototype of the built-in ``render_partial`` (D10): a mapping
    from names to other ``PageTemplate`` objects, which ``render_partial(name, **kwargs)``
    renders as native frames when no ``render_partial`` is passed to ``render``. Phase 9
    replaces it with path resolution.
    """

    translate: ClassVar[Callable[..., Any]] = staticmethod(simple_translate)
    on_error_handler: Callable[[BaseException], object] | None = None
    encoding: str | None = None

    def __init__(
        self,
        body: str,
        *,
        filename: str = '<string>',
        partials: Mapping[str, PageTemplate] | None = None,
    ) -> None:
        self.body = body
        self.filename = filename
        self._program = Program(body, filename)
        self._partials = partials
        self._defaults = self._render_defaults(None, None)

    def _partial_programs(self) -> dict[str, Program] | None:
        if self._partials is None:
            return None
        return {name: template._program for name, template in self._partials.items()}

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
        return self._program.render(kw, partials, defaults)

    __call__ = render
