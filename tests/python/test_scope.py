"""The native Scope (D5) against Chameleon's documented behaviour (utils.py Scope doctest)."""

import gc
import platform
import weakref

import pytest
from chameleon.utils import Scope as ChameleonScope
from chameleon_rs._native import Scope


def test_doctest_semantics() -> None:
    scope = Scope()
    scope['a'] = 1
    copy = scope.copy()

    # A local value takes precedence over a global one.
    copy['a'] = 2
    copy.set_global('a', 3)
    assert copy['a'] == 2

    # A new global value is immediately visible.
    copy.set_global('b', 1)
    assert copy['b'] == 1


def test_reference_counted_without_cycles() -> None:
    # Mirrors the upstream doctest: nothing is left for the cycle collector.
    gc.collect()
    scope = Scope()
    scope['a'] = 1
    copy = scope.copy()
    copy['a'] = 2
    copy.set_global('b', 1)
    del copy
    del scope
    assert gc.collect() == (0 if platform.python_implementation() == 'CPython' else None)


class Sentinel:
    pass


def test_cycles_through_root_are_collected() -> None:
    # Like Chameleon's Scope, ours is not weak-referenceable; watch an object it holds.
    scope = Scope()
    child = scope.copy()
    scope['child'] = child  # root -> child -> root (child holds root as a Rust field)
    sentinel = Sentinel()
    child['sentinel'] = sentinel
    ref = weakref.ref(sentinel)
    del scope, child, sentinel
    gc.collect()
    assert ref() is None


@pytest.mark.parametrize('cls', [Scope, ChameleonScope])
def test_mapping_behaviour_matches(cls: type) -> None:
    root = cls(a=1, b=2)
    child = root.copy()
    child['c'] = 3
    child['a'] = 10
    assert sorted(child) == ['a', 'b', 'c']
    assert child['b'] == 2
    assert 'b' in child
    assert 'z' not in child
    assert child.get('z') is None
    assert child.get('z', 5) == 5
    assert child.get_name('c') == 3
    with pytest.raises(NameError):
        child.get_name('z')
    with pytest.raises(KeyError):
        child['z']
    assert child.vars is child
    assert isinstance(child, dict)
    child.set_local('d', 4)
    assert child['d'] == 4
    assert dict.__len__(child) == 4
