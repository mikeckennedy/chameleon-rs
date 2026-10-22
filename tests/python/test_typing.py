"""PEP 561 typing support: the package ships ``py.typed`` and the extension's stubs, the stubs
name everything the extension exports, and the public API is fully annotated."""

from __future__ import annotations

import ast
import importlib
import inspect
import typing
from pathlib import Path
from types import ModuleType
from typing import Any

import chameleon_rs
import chameleon_rs._native
import pytest

PACKAGE = Path(chameleon_rs.__file__).parent

# The public modules and the names each exports (`__all__`, or the listed names).
PUBLIC: dict[str, tuple[str, ...] | None] = {
    'chameleon_rs': None,
    'chameleon_rs._async': None,
    'chameleon_rs.compat': ('install', 'installed'),
    'chameleon_rs.flask': None,
    'chameleon_rs.partials': None,
    'chameleon_rs.shadow': None,
    'chameleon_rs.template': None,
    'chameleon_rs.web': None,
    'chameleon_rs.zpt.template': None,
}


def _stub_names() -> set[str]:
    tree = ast.parse((PACKAGE / '_native.pyi').read_text(encoding='utf-8'))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_package_ships_py_typed_and_stubs() -> None:
    assert (PACKAGE / 'py.typed').is_file()
    assert (PACKAGE / '_native.pyi').is_file()


def test_stubs_name_everything_the_extension_exports() -> None:
    exported = {name for name in dir(chameleon_rs._native) if not name.startswith('__')}
    assert exported - _stub_names() == set()


def _exports(module_name: str) -> list[tuple[str, Any]]:
    module = importlib.import_module(module_name)
    names = PUBLIC[module_name] or tuple(module.__all__)
    return [(name, getattr(module, name)) for name in names]


def _unannotated(func: Any, hints_of: Any) -> list[str]:
    """The parameters (and ``return``) of ``func`` without an annotation."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # a native callable: covered by the stubs
        return []
    hints = typing.get_type_hints(hints_of)
    missing = [
        p.name
        for i, p in enumerate(signature.parameters.values())
        if p.name not in hints and not (i == 0 and p.name in ('self', 'cls'))
    ]
    if 'return' not in hints:
        missing.append('return')
    return missing


def _public_functions(obj: Any) -> list[tuple[str, Any]]:
    if isinstance(obj, type):
        if obj.__module__ == 'chameleon_rs._native':
            return []
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
        if isinstance(obj, ModuleType):
            continue
        for label, func in _public_functions(obj):
            unannotated = _unannotated(func, func)
            if unannotated:
                missing[label] = unannotated
    assert missing == {}
