"""PEP 561 typing support: the package ships ``py.typed`` and the extension's stubs, the stubs
match what the extension exports (names, parameters and defaults), and the public API is fully
annotated and its annotations resolve."""

from __future__ import annotations

import ast
import importlib
import inspect
import types
import typing
from pathlib import Path
from typing import Any

import chameleon_rs
import chameleon_rs._native as native
import pytest

PACKAGE = Path(chameleon_rs.__file__).parent

# The public modules and the names each exports: `__all__`, the listed names, or (`None` and
# no `__all__`) the public functions and classes the module defines.
PUBLIC: dict[str, tuple[str, ...] | None] = {
    'chameleon_rs': None,
    'chameleon_rs._async': None,
    'chameleon_rs.compat': ('install', 'installed'),
    'chameleon_rs.exc': None,
    'chameleon_rs.fastapi': None,
    'chameleon_rs.flask': None,
    'chameleon_rs.loader': None,
    'chameleon_rs.partials': None,
    'chameleon_rs.robyn': None,
    'chameleon_rs.shadow': None,
    'chameleon_rs.starlette': None,
    'chameleon_rs.template': None,
    'chameleon_rs.web': None,
    'chameleon_rs.zpt.loader': None,
    'chameleon_rs.zpt.template': None,
}

# --- The stubs ----------------------------------------------------------------------------

STUB = ast.parse((PACKAGE / '_native.pyi').read_text(encoding='utf-8'))
# Stub-only helper classes (`@type_check_only`), not in the extension.
STUB_ONLY = {'_TestingModule'}


def _stub_classes() -> dict[str, ast.ClassDef]:
    return {node.name: node for node in STUB.body if isinstance(node, ast.ClassDef)}


def _stub_names() -> set[str]:
    names: set[str] = set()
    for node in STUB.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names - STUB_ONLY


def _members(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}


def _is_property(func: ast.FunctionDef) -> bool:
    return any(isinstance(d, ast.Name) and d.id == 'property' for d in func.decorator_list)


def _stub_params(func: ast.FunctionDef, method: bool) -> list[tuple[str, Any, object]]:
    """``(name, kind, default)`` per parameter (without ``self`` for a method)."""
    a = func.args
    positional: list[tuple[ast.arg, inspect._ParameterKind]] = [
        (p, inspect.Parameter.POSITIONAL_ONLY) for p in a.posonlyargs
    ]
    positional += [(p, inspect.Parameter.POSITIONAL_OR_KEYWORD) for p in a.args]
    defaults: list[object] = [inspect.Parameter.empty] * (len(positional) - len(a.defaults))
    defaults += [ast.literal_eval(d) for d in a.defaults]
    out: list[tuple[str, Any, object]] = [(p.arg, kind, d) for (p, kind), d in zip(positional, defaults, strict=True)]
    if a.vararg:
        out.append(('*', inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.empty))
    for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        default = inspect.Parameter.empty if d is None else ast.literal_eval(d)
        out.append((p.arg, inspect.Parameter.KEYWORD_ONLY, default))
    if a.kwarg:
        out.append(('**', inspect.Parameter.VAR_KEYWORD, inspect.Parameter.empty))
    return out[1:] if method else out


def _runtime_params(obj: Any, method: bool) -> list[tuple[str, Any, object]] | None:
    """The same from PyO3's text signature; ``None`` when it says nothing (a slot wrapper
    taking ``*args, **kwargs``, like ``__call__``)."""
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    params = list(signature.parameters.values())[1 if method else 0 :]
    kinds = [p.kind for p in params]
    if isinstance(obj, types.WrapperDescriptorType) and kinds == [
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ]:
        return None
    names = {inspect.Parameter.VAR_POSITIONAL: '*', inspect.Parameter.VAR_KEYWORD: '**'}
    return [(names.get(p.kind, p.name), p.kind, p.default) for p in params]


def test_package_ships_py_typed_and_stubs() -> None:
    assert (PACKAGE / 'py.typed').is_file()
    assert (PACKAGE / '_native.pyi').is_file()


def test_stub_names_match_the_extension() -> None:
    exported = {name for name in dir(native) if not name.startswith('__')}
    assert exported == _stub_names()
    testing = {name for name in dir(native._testing) if not name.startswith('__')}
    assert testing == set(_members(_stub_classes()['_TestingModule']))


def test_stub_class_members_match_the_extension() -> None:
    differences = {}
    for name, cls in _stub_classes().items():
        if name in STUB_ONLY:
            continue
        runtime = getattr(native, name)
        stubbed = set(_members(cls))
        stale = {m for m in stubbed if not hasattr(runtime, m)}
        missing = {m for m in vars(runtime) if not m.startswith('_')} - stubbed
        if stale or missing:
            differences[name] = (stale, missing)
    assert differences == {}


def _signature_pairs() -> list[tuple[str, Any, ast.FunctionDef, bool]]:
    pairs = []
    for node in STUB.body:
        if isinstance(node, ast.FunctionDef):
            pairs.append((node.name, getattr(native, node.name), node, False))
    for name, cls in _stub_classes().items():
        members = _members(cls)
        if name == '_TestingModule':
            pairs.extend((f'_testing.{m}', getattr(native._testing, m), f, False) for m, f in members.items())
            continue
        runtime = getattr(native, name)
        for member, func in members.items():
            if _is_property(func):
                continue
            if member == '__init__':
                # The constructor's signature (PyO3's `#[new]`) is the class's.
                pairs.append((f'{name}()', runtime, func, True))
            else:
                pairs.append((f'{name}.{member}', vars(runtime).get(member, getattr(runtime, member)), func, True))
    return pairs


def test_stub_signatures_match_the_extension() -> None:
    differences = {}
    checked = 0
    for label, runtime, func, method in _signature_pairs():
        got = _runtime_params(runtime, method and not isinstance(runtime, type))
        if got is None:
            continue
        checked += 1
        want = _stub_params(func, method)
        if got != want:
            differences[label] = (want, got)
    assert differences == {}
    assert checked > 30


# --- The public API -----------------------------------------------------------------------


# Public modules whose framework is not installable on every Python we test: Robyn ships wheels
# for CPython 3.10-3.14 only.
NEEDS = {'chameleon_rs.robyn': 'robyn'}


def _exports(module_name: str) -> list[tuple[str, Any]]:
    if module_name in NEEDS:
        pytest.importorskip(NEEDS[module_name])
    module = importlib.import_module(module_name)
    names = PUBLIC[module_name] or getattr(module, '__all__', None)
    if names is None:
        names = [
            name
            for name, value in vars(module).items()
            if not name.startswith('_')
            and (inspect.isfunction(value) or inspect.isclass(value))
            and value.__module__ == module_name
        ]
    return [(name, getattr(module, name)) for name in names]


def _unannotated(func: Any) -> list[str]:
    """The parameters (and ``return``) of ``func`` without an annotation."""
    signature = inspect.signature(func)
    annotations = inspect.get_annotations(func)
    missing = [
        p.name
        for i, p in enumerate(signature.parameters.values())
        if p.name not in annotations and not (i == 0 and p.name in ('self', 'cls'))
    ]
    if 'return' not in annotations:
        missing.append('return')
    return missing


def _public_functions(obj: Any) -> list[tuple[str, Any]]:
    if isinstance(obj, type):
        if obj.__module__ == 'chameleon_rs._native':
            return []  # covered by the stub tests
        out = []
        for name, value in vars(obj).items():
            if name.startswith('_') and name not in ('__init__', '__call__'):
                continue
            func = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
            if isinstance(func, property):
                func = func.fget
            if inspect.isfunction(func):
                out.append((f'{obj.__name__}.{name}', func))
        return out
    if inspect.isfunction(obj):
        return [(obj.__name__, obj)]
    return []


@pytest.mark.parametrize('module_name', sorted(PUBLIC))
def test_public_api_is_annotated(module_name: str) -> None:
    missing = {}
    for _name, obj in _exports(module_name):
        for label, func in _public_functions(obj):
            unannotated = _unannotated(func)
            if unannotated:
                missing[label] = unannotated
    assert missing == {}


@pytest.mark.parametrize('module_name', sorted(PUBLIC))
def test_public_class_annotations_resolve(module_name: str) -> None:
    # `typing.get_type_hints` evaluates the class-level annotations (and its bases'): each must
    # be valid at runtime on every supported Python (`staticmethod[...]` is not before 3.14).
    for _name, obj in _exports(module_name):
        if isinstance(obj, type) and obj.__module__ != 'chameleon_rs._native':
            typing.get_type_hints(obj)
