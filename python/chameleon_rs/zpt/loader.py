# Ported from Chameleon's src/chameleon/zpt/loader.py (`TemplateLoader`), modified: type
# annotations; paths may be `os.PathLike` (malthe/chameleon#342).
"""``TemplateLoader`` (exported as ``chameleon_rs.PageTemplateLoader``): loads page templates
by path spec, in the ``xml`` or ``text`` format."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from chameleon_rs.loader import TemplateLoader as BaseLoader
from chameleon_rs.template import PageTemplateFile, PageTextTemplateFile


class TemplateLoader(BaseLoader):
    """Loads and caches page templates from one or more directories by filename (``loader['page.pt']``).

    Accepts a single directory path (``str`` or ``os.PathLike``) or an iterable of paths as
    ``search_path``, and passes extra keyword arguments (such as ``auto_reload`` or
    ``strict_tags``) to each template it constructs.
    """

    # A class attribute that the `formats` argument replaces for one instance.
    formats: Mapping[str, type[PageTemplateFile]] = {
        'xml': PageTemplateFile,
        'text': PageTextTemplateFile,
    }

    default_format = 'xml'

    def __init__(
        self,
        search_path: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
        default_extension: str | None = None,
        *,
        formats: Mapping[str, type[PageTemplateFile]] | None = None,
        **kwargs: Any,
    ) -> None:
        if formats is not None:
            self.formats = formats

        super().__init__(search_path, default_extension, **kwargs)

    # Chameleon names the argument `filename` here and `spec` in the base class.
    def load(  # ty: ignore[invalid-method-override]
        self, filename: str | os.PathLike[str], format: str | None = None
    ) -> PageTemplateFile:
        """Load and return a template file.

        The format parameter determines will parse the file. Valid options are `xml` and
        `text`.
        """

        cls = self.formats[format or self.default_format]
        return super().load(filename, cls)  # type: ignore[no-any-return]

    __getitem__ = load
