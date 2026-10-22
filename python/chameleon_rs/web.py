"""Shared pieces of the web framework integrations (``chameleon_rs.flask``,
``chameleon_rs.fastapi``, ``chameleon_rs.starlette``): the exceptions every integration raises, and the conversion of a
view's return value to a template model. Imports only the standard library.

Each integration's old exception names (``FlaskChameleonException``, ...) are subclasses of
these, so ``except chameleon_rs.web.TemplateError`` catches all of them and old ``except``
clauses keep working.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any


class TemplateError(Exception):
    """A template integration was configured or used incorrectly: no template folder
    configured, a folder that does not exist, a view returning something that is not a model
    or a response. (Not a template compile error: that is ``chameleon_rs.TemplateError``.)"""


class ErrorPage(TemplateError):
    """Raised inside a decorated view to answer with an error page: the decorator renders
    ``template_file`` with ``template_data`` and ``status_code``."""

    def __init__(
        self,
        template_file: str,
        status_code: int,
        message: str | None = None,
        template_data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.template_file: str = template_file
        self.status_code: int = status_code
        self.message: str | None = message
        self.template_data: Mapping[str, Any] | None = template_data


class NotFound(ErrorPage):
    """Raised by ``not_found()``: the decorator renders ``template_file`` (``errors/404.pt``
    by default) with status 404."""

    def __init__(
        self,
        message: str | None = None,
        template_file: str = 'errors/404.pt',
        template_data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(template_file, 404, message, template_data)


class DecoratorOrderError(TemplateError):
    """A view was registered with its framework before ``@template`` wrapped it (``@template``
    written above ``@app.get`` / ``@app.route`` / ``@router.get``), so the route calls the bare
    view and the template never renders. Raised when the app starts (or on its first request),
    for apps given to ``configure(app=...)`` or ``global_init(..., app=...)``, and by
    ``check_views(app)``."""


def model_of(value: Any) -> dict[str, Any] | None:
    """The template model a view returned: a ``dict`` as is; any other ``Mapping`` as a
    ``dict``; a dataclass instance as its fields (shallow: field values are not converted);
    an object with a callable ``model_dump()`` (Pydantic, duck-typed) as what it returns.
    ``None`` for anything else."""
    if type(value) is dict:
        return value
    if isinstance(value, Mapping):
        return dict(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    dump = getattr(value, 'model_dump', None)
    if callable(dump) and not isinstance(value, type):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return None


__all__ = ['DecoratorOrderError', 'ErrorPage', 'NotFound', 'TemplateError', 'model_of']
