# Ported from Chameleon's src/chameleon/zpt/template.py (PageTemplate.render's setup of
# `__translate`, `__decode`, `__on_error_handler`, `target_language` and `repeat`;
# `PageTemplate.parse`, `BOOLEAN_HTML_ATTRIBUTES`, `PageTextTemplate`) and template.py
# (`BaseTemplate.write`), modified: the template compiles to a native program instead of a
# Python module.
"""The public template classes (D7): ``PageTemplate(body)`` and ``PageTextTemplate(body)``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from chameleon_rs._native import Program
from chameleon_rs.i18n import simple_translate
from chameleon_rs.tal import RepeatDict
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
        self._partials = partials
        self.write(body)
        self._defaults = self._render_defaults(None, None)

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
        self.content_type = content_type or self.default_content_type
        self.content_encoding = encoding
        self.body = body
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
            self.filename,
            mode=self.mode,
            boolean_attributes=boolean_attributes or (),
            **options,
        )

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


class PageTextTemplate(PageTemplate):
    """A text template: only `${...}` interpolation, no markup, nothing escaped."""

    mode = 'text'
