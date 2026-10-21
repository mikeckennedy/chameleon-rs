"""Helpers for the expression differential tests: evaluate one TALES expression in a scope
with each engine and describe the outcome so the two can be compared."""

from __future__ import annotations

import re
from typing import Any

import chameleon
import chameleon_rs

ADDRESS = re.compile(r'0x[0-9a-fA-F]+')
RCONTEXT_ERRORS = re.compile(r"\{'__error__': \[.*?\]\}")


def attribute_text(expression: str) -> str:
    """`expression` as the value of a `tal:define` clause: the value is entity-decoded first,
    then split at `;` (`;;` is a literal semicolon)."""
    return expression.replace(';', ';;').replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')


def canon(text: str) -> str:
    """Object addresses and the engine's package name differ between the engines."""
    text = ADDRESS.sub('0x?', text)
    # Chameleon records render errors in `rcontext['__error__']` (Phase 6a's error tracking),
    # visible when the exception holds `rcontext` itself.
    text = RCONTEXT_ERRORS.sub('{}', text)
    # A few messages print the scope's type fully qualified (`module.qualname`); ours is a
    # native class in `chameleon_rs._native`, Chameleon's the Python class in `chameleon.utils`.
    text = text.replace('chameleon_rs._native.Scope', 'chameleon.utils.Scope')
    return text.replace('chameleon_rs.', 'chameleon.')


def describe_value(value: Any) -> tuple[str, str]:
    return type(value).__name__, canon(repr(value))


def describe_error(error: BaseException) -> tuple[str, str, str]:
    # Chameleon wraps render errors in a `RenderError` subclass named like the original.
    args = error.args if not isinstance(error, (chameleon.exc.TemplateError, chameleon_rs.exc.TemplateError)) else ()
    if isinstance(error, SyntaxError):
        # Chameleon's location points into its generated module, ours into the expression
        # (a documented difference); on some Python versions it is set, on others not.
        args = (error.msg,)
    return 'raise', type(error).__name__, canon(repr(args))


def evaluate(engine: Any, expression: str, scope: dict[str, Any], template_class: Any = None) -> tuple[str, ...]:
    """The value of `expression` defined in a scope (passed to a sink), or the error."""
    out: list[Any] = []

    def sink(value: Any) -> str:
        out.append(value)
        return ''

    source = f'<tal:a define="v {attribute_text(expression)}"><tal:b define="_ sink(v)"/></tal:a>'
    cls = template_class or engine.PageTemplate
    try:
        cls(source)(sink=sink, **scope)
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return describe_error(e)
    return ('value', *describe_value(out[0]))


def render(engine: Any, source: str, scope: dict[str, Any], template_class: Any = None, **options: Any) -> Any:
    cls = template_class or engine.PageTemplate
    try:
        return ('ok', cls(source, **options)(**scope))
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return describe_error(e)
