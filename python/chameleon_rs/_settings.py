"""Process-wide settings for templates loaded by name: ``chameleon_rs.configure()``.

One call configures the template folder and its loader for everything that loads templates by
name: the built-in ``render_partial`` and the framework integrations (``chameleon_rs.flask``,
``chameleon_rs.starlette``, ``chameleon_rs.fastapi``, ``chameleon_rs.robyn``). The old per-library calls
(``chameleon_partials.register_extensions``, ``chameleon_flask.global_init``,
``fastapi_chameleon.global_init``, ``chameleon_robyn.global_init``) are aliases of it, each
setting only its own slot. A configuration is an immutable ``Settings`` object swapped in
whole, so a render never sees half of one (free-threading).
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any, Literal, TypedDict

from chameleon_rs import _native
from chameleon_rs.web import TemplateError


class MarkdownExtensions(TypedDict, total=False):
    """The extensions of a ``markdown:`` profile (all on by default)."""

    table: bool
    strikethrough: bool
    tasklist: bool
    autolink: bool
    footnotes: bool


class MarkdownOptions(TypedDict, total=False):
    """The options of a ``markdown:`` profile, as ``configure(markdown={prefix: options})`` takes
    them (see the docs); a key left out keeps its default."""

    raw_html: Literal['escape', 'allow']
    images: bool
    inline: bool
    heading_ids: bool | str
    classes: Mapping[str, str]
    extensions: MarkdownExtensions


type MarkdownSetting = bool | Mapping[str, Literal[True] | MarkdownOptions]
"""``configure(markdown=...)``: ``True`` (``markdown:`` with the defaults), ``False`` (none) or
a mapping of profiles, prefix -> ``True`` (the defaults) or options."""


class Settings:
    """A configuration: the template folder, its loader, and the loader's options."""

    __slots__ = ('auto_reload', 'loader', 'restricted_namespace', 'template_folder')

    def __init__(self, template_folder: str, auto_reload: bool, restricted_namespace: bool) -> None:
        from chameleon_rs.zpt.loader import TemplateLoader

        self.template_folder = template_folder
        self.auto_reload = auto_reload
        self.restricted_namespace = restricted_namespace
        kwargs: dict[str, Any] = {'auto_reload': auto_reload}
        if not restricted_namespace:
            kwargs['restricted_namespace'] = False
        self.loader = TemplateLoader(template_folder, **kwargs)

    def __repr__(self) -> str:
        return (
            f'Settings(template_folder={self.template_folder!r}, auto_reload={self.auto_reload!r}, '
            f'restricted_namespace={self.restricted_namespace!r})'
        )


# The process-wide configuration, one slot per consumer, as the original libraries each had
# their own: `partials` (what `render_partial` resolves against; chameleon_partials'
# `register_extensions`) and one per framework integration in `views`, keyed by the names in
# `VIEW_SLOTS` (chameleon-flask's, fastapi-chameleon's and chameleon-robyn's `global_init`). `configure()` sets
# every slot, each alias only its own (`None`: not configured, or cleared). `views` is replaced
# whole, never mutated. `generation` is bumped by every change and keys the partial path caches.
VIEW_SLOTS = ('flask', 'starlette', 'fastapi', 'robyn')
partials: Settings | None = None
views: dict[str, Settings | None] = dict.fromkeys(VIEW_SLOTS)
generation = 0
_lock = threading.Lock()
# Called after every change, keyed by module name (so a re-imported module replaces its own):
# the integrations keep their legacy `template_path` in step.
listeners: dict[str, Callable[[], None]] = {}

# Where `render_partial` looks: `'caller'` (the default, approved by Michael: the registered
# folder, then the calling template's search path, its folder and each folder above it; it only
# ever turns chameleon_partials' "not found" error into a render) or `'registered'`
# (chameleon_partials' own lookup: the registered folder only).
PARTIAL_SEARCH = ('caller', 'registered')
DEFAULT_SEARCH = 'caller'
search_mode = DEFAULT_SEARCH

# The `markdown:` expression types (Phase 11b): prefix -> native profile, built once by
# `configure(markdown=...)` and added to each template's expression types when it compiles
# (`chameleon_rs.tales.native_types`), unless the template class defines the prefix itself.
# Replaced whole, never mutated.
markdown_profiles: dict[str, _native.MarkdownProfile] = {}
DEFAULT_MARKDOWN_CACHE: int = _native.markdown_cache_limits()[0]
markdown_cache_size = DEFAULT_MARKDOWN_CACHE

# A prefix as Chameleon matches it (`tales.match_prefix`), and Chameleon's own prefixes, which a
# Markdown profile may not take.
_PREFIX = re.compile(r'[a-z][a-z0-9\-_]*')
_ID_PREFIX = re.compile(r'[A-Za-z0-9_-]*')
_RESERVED = frozenset({'python', 'string', 'not', 'exists', 'import', 'structure', 'load'})
_MARKDOWN_OPTIONS = frozenset({'raw_html', 'images', 'inline', 'heading_ids', 'classes', 'extensions'})
_EXTENSIONS = ('table', 'strikethrough', 'tasklist', 'autolink', 'footnotes')


def _markdown_profile(name: object, options: Any) -> _native.MarkdownProfile:
    """One validated profile: ``options`` is ``True`` (the defaults) or a mapping of them."""
    if not isinstance(name, str) or not _PREFIX.fullmatch(name):
        msg = f'markdown: profile names are expression prefixes (lowercase, like "md-comment"), not {name!r}'
        raise ValueError(msg)
    if name in _RESERVED:
        msg = f"markdown: {name!r} is one of Chameleon's own expression types"
        raise ValueError(msg)
    if options is True:
        options = {}
    if not isinstance(options, Mapping):
        msg = f'markdown: the options for {name!r} must be a dict or True, not {options!r}'
        raise TypeError(msg)
    unknown = set(options) - _MARKDOWN_OPTIONS
    if unknown:
        msg = f'markdown: unknown option {min(unknown)!r} for {name!r} (one of {", ".join(sorted(_MARKDOWN_OPTIONS))})'
        raise ValueError(msg)

    def flag(key: str, default: bool) -> bool:
        value = options.get(key, default)
        if not isinstance(value, bool):
            msg = f'markdown: {key} for {name!r} must be True or False, not {value!r}'
            raise TypeError(msg)
        return value

    heading_ids = options.get('heading_ids', False)
    if heading_ids is True:
        heading_ids = ''
    elif heading_ids is False:
        heading_ids = None
    elif not isinstance(heading_ids, str):
        msg = f'markdown: heading_ids for {name!r} must be True, False or a prefix string, not {heading_ids!r}'
        raise TypeError(msg)
    elif not _ID_PREFIX.fullmatch(heading_ids):
        # comrak writes the prefix into `id` and `href` as it is.
        msg = f'markdown: heading_ids for {name!r} must be letters, digits, "-" and "_", not {heading_ids!r}'
        raise ValueError(msg)
    classes = options.get('classes', {})
    if not isinstance(classes, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in classes.items()
    ):
        msg = f'markdown: classes for {name!r} must map element names to class strings, not {classes!r}'
        raise TypeError(msg)
    extensions = options.get('extensions', {})
    if not isinstance(extensions, Mapping):
        msg = f'markdown: extensions for {name!r} must be a dict, not {extensions!r}'
        raise TypeError(msg)
    if not set(extensions) <= set(_EXTENSIONS):
        msg = f'markdown: extensions for {name!r} must map some of {", ".join(_EXTENSIONS)} to True or False, not {extensions!r}'
        raise ValueError(msg)
    switches = {}
    for key in _EXTENSIONS:
        value = extensions.get(key, True)
        if not isinstance(value, bool):
            msg = f'markdown: extensions[{key!r}] for {name!r} must be True or False, not {value!r}'
            raise TypeError(msg)
        switches[key] = value
    return _native.MarkdownProfile(
        name,
        raw_html=options.get('raw_html', 'escape'),
        images=flag('images', True),
        inline=flag('inline', False),
        heading_ids=heading_ids,
        classes=list(classes.items()),
        **switches,
    )


def markdown_settings(markdown: object) -> dict[str, _native.MarkdownProfile]:
    """``configure(markdown=...)``'s value as profiles: ``True`` is ``{'markdown': {}}``,
    ``False`` none, a mapping one profile per prefix."""
    if markdown is True:
        markdown = {'markdown': {}}
    elif markdown is False:
        markdown = {}
    if not isinstance(markdown, Mapping):
        msg = f'markdown must be True, False or a dict of profiles, not {markdown!r}'
        raise TypeError(msg)
    return {name: _markdown_profile(name, options) for name, options in markdown.items()}


def check_folder(template_folder: str | os.PathLike[str] | None, error: type[Exception] = TemplateError) -> None:
    """chameleon_partials' and chameleon-flask's checks, with their messages."""
    if not template_folder:
        msg = 'The template_folder must be specified.'
        raise error(msg)
    if not os.path.isdir(template_folder):
        msg = f"The specified template folder must be a folder, it's not: {template_folder}"
        raise error(msg)


def build(
    template_folder: str | os.PathLike[str] | None = None,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    *,
    base: Settings | None = None,
    error: type[Exception] = TemplateError,
) -> Settings:
    """A configuration from ``base`` with the given settings changed (``None``: unchanged)."""
    folder = template_folder if template_folder is not None else (base.template_folder if base else None)
    check_folder(folder, error)
    if auto_reload is None:
        auto_reload = base.auto_reload if base else False
    if restricted_namespace is None:
        restricted_namespace = base.restricted_namespace if base else True
    return Settings(str(folder), bool(auto_reload), bool(restricted_namespace))


_KEEP: Any = object()


def first_configured() -> Settings | None:
    """The configuration a ``configure()`` that leaves a setting out starts from: the partials'
    slot, else the first configured integration's."""
    if partials is not None:
        return partials
    return next((settings for settings in views.values() if settings is not None), None)


def install(*, partials_settings: Any = _KEEP, views_settings: Mapping[str, Settings | None] | None = None) -> None:
    """Sets the slots given (``None`` clears one): ``views_settings`` maps integration slot names
    (``VIEW_SLOTS``) to their new configuration."""
    global partials, views, generation
    with _lock:
        if partials_settings is not _KEEP:
            partials = partials_settings
        if views_settings:
            unknown = set(views_settings) - set(VIEW_SLOTS)
            if unknown:
                msg = f'unknown settings slot {min(unknown)!r}'
                raise ValueError(msg)
            views = {**views, **views_settings}
        generation += 1
    from chameleon_rs import partials as partials_module

    partials_module._configured(partials)
    for listener in list(listeners.values()):
        listener()


def all_views(settings: Settings | None) -> dict[str, Settings | None]:
    """Every integration slot set to ``settings``."""
    return dict.fromkeys(VIEW_SLOTS, settings)


def configure(
    template_folder: str | os.PathLike[str] | None = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    partial_search: str | None = None,
    markdown: MarkdownSetting | None = None,
    markdown_cache: int | None = None,
) -> Settings | None:
    """Configures chameleon-rs for the whole process: where templates loaded by name live, for
    ``render_partial`` and the framework integrations (``chameleon_rs.flask``,
    ``chameleon_rs.starlette``, ``chameleon_rs.fastapi``, ``chameleon_rs.robyn``), and how they load.

    ``template_folder``: an existing directory. ``auto_reload``: recompile a template when its
    file changes (checked at most once per file per render for partials). ``restricted_namespace``:
    ``False`` allows attributes such as Alpine.js's ``@click`` (chameleon-flask's option).
    ``partial_search``: ``'caller'`` (the default: partials are looked for in the template
    folder, then on the calling template's search path, then in its folder and each folder above
    it) or ``'registered'`` (chameleon_partials' lookup: the template folder only, a path not
    there being an error). ``markdown``: the ``markdown:`` expression types, ``True`` for
    ``markdown:`` with the defaults or a dict of profiles (prefix -> options, see the docs),
    ``False`` for none; it applies to templates compiled afterwards. ``markdown_cache``: how many
    conversions to keep (``0``: no cache). A setting left out keeps its current value; the first
    call that sets the folder's settings must give the folder (``partial_search`` and the
    Markdown settings don't need one). Applies every time it is called (there is no
    ``cache_init``).

    Raises ``chameleon_rs.web.TemplateError`` for a missing folder, ``ValueError`` for an
    unknown ``partial_search`` or an invalid Markdown setting, ``TypeError`` for a Markdown
    setting of the wrong type (nothing changes then).
    """
    global search_mode, markdown_profiles, markdown_cache_size
    # Everything is checked before anything changes.
    if partial_search is not None and partial_search not in PARTIAL_SEARCH:
        msg = f'partial_search must be one of {PARTIAL_SEARCH}, not {partial_search!r}'
        raise ValueError(msg)
    if markdown_cache is not None:
        if isinstance(markdown_cache, bool) or not isinstance(markdown_cache, int):
            msg = f'markdown_cache must be a number of entries (0 or more), not {markdown_cache!r}'
            raise TypeError(msg)
        if not 0 <= markdown_cache <= sys.maxsize:
            msg = f'markdown_cache must be a number of entries (0 or more), not {markdown_cache!r}'
            raise ValueError(msg)
    profiles = markdown_settings(markdown) if markdown is not None else None
    folder_given = template_folder is not None or auto_reload is not None or restricted_namespace is not None
    if folder_given:
        settings = build(template_folder, auto_reload, restricted_namespace, base=first_configured())
    if profiles is not None or markdown_cache is not None:
        size = markdown_cache_size if markdown_cache is None else markdown_cache
        # First, so a failure leaves the settings as they were. It also empties the cache:
        # conversions by an earlier configuration's profiles are dropped.
        _native.set_markdown_cache(size)
        markdown_cache_size = size
        if profiles is not None:
            markdown_profiles = profiles
    if partial_search is not None:
        search_mode = partial_search
    if not folder_given:
        if partial_search is not None or profiles is not None or markdown_cache is not None:
            install()
            return partials
        settings = build(base=first_configured())
    install(partials_settings=settings, views_settings=all_views(settings))
    return settings


def clear() -> None:
    """Forgets the process-wide configuration, both slots and ``partial_search`` (tests reset
    with it), and the Markdown settings."""
    global search_mode, markdown_profiles, markdown_cache_size
    search_mode = DEFAULT_SEARCH
    markdown_profiles = {}
    markdown_cache_size = DEFAULT_MARKDOWN_CACHE
    _native.set_markdown_cache(DEFAULT_MARKDOWN_CACHE)
    install(partials_settings=None, views_settings=all_views(None))
