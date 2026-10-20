# Ported from Chameleon's src/chameleon/exc.py (the TemplateError hierarchy), modified: errors
# carry a code-point offset and location computed by the Rust core instead of a Token, and
# the message format is simplified until Phase 7 ports ExceptionFormatter.
"""Exceptions raised by chameleon-rs."""

from __future__ import annotations


class TemplateError(Exception):
    """An error raised by chameleon-rs while compiling a template."""

    def __init__(
        self,
        msg: str,
        token: str = '',
        offset: int = 0,
        location: tuple[int, int] = (0, 0),
        filename: str = '',
    ) -> None:
        Exception.__init__(self, msg, token)
        self.msg = msg
        self.token = token
        self.offset = offset
        self.location = location
        self.filename = filename

    def __str__(self) -> str:
        text = f'{self.msg}\n\n'
        text += f' - String:     "{self.token}"'
        if self.filename:
            text += f'\n - Filename:   {self.filename}'
        lineno, column = self.location
        text += f'\n - Location:   (line {lineno}: col {column})'
        return text


class ParseError(TemplateError):
    """An error occurred during parsing (structural level)."""


class CompilationError(TemplateError):
    """An error occurred during compilation."""


class TranslationError(TemplateError):
    """An error occurred during translation."""


class LanguageError(CompilationError):
    """Incorrect usage of the template language."""


class ExpressionError(LanguageError):
    """An error occurred compiling an expression."""


class UnsupportedError(CompilationError, NotImplementedError):
    """Valid Chameleon that this version of chameleon-rs does not support yet."""


class InternalError(RuntimeError):
    """A bug in chameleon-rs (a Rust panic), raised instead of crashing the process."""
