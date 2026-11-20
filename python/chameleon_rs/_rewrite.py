# Ported from Chameleon's src/chameleon/compiler.py (NameTransform, the render function's
# bindings of `translate`, `decode` and `on_error_handler`, `visit_CodeBlock`), astutil.py
# (NameLookupRewriteVisitor, ItemLookupOnAttributeErrorVisitor) and tales.py
# (transform_attribute), modified: the rewritten expression becomes a function taking the
# scope, instead of code inlined into a generated render function; code blocks and the
# expressions that use their local names run in a per-render namespace.
"""The exact fallback for Python expressions the native fast path does not cover (D4)."""

from __future__ import annotations

import ast
import builtins
import contextlib
import dis
import sys
import sysconfig
import textwrap
import warnings
from collections.abc import Callable, Generator, Iterable, Iterator
from typing import Any, Optional, cast

# Names the compiler keeps for itself; expressions see the render's own objects.
INTERNALS = frozenset({'econtext', 'rcontext'})

# Locals of every Chameleon render function, bound from the scope when it starts. Expressions
# read them as plain names (a comprehension may even rebind them locally).
DEFAULTS = {'translate': '__translate', 'decode': '__decode', 'on_error_handler': '__on_error_handler'}

BUILTINS = frozenset(builtins.__dict__)

# The parameters of a compiled expression: the scope, the render context, the default marker
# (the `default` alias) and the template builtins' values.
PARAMS = ('econtext', 'rcontext', '__rs_default', '__rs_tpl')


def _name_error(name: str) -> Any:
    raise NameError(f'name {name!r} is not defined')


# Module globals of Chameleon's generated code that a `__` name in an expression can reach,
# and the helper that keeps our own names (`__rs_*`, `__lookup_attr`) out of reach.
# The plain name a `match` statement's patterns read the scope's names through (see
# `Rewriter.visit_Match`). Ours (`_ours`), and not mangled in a class: it ends in `__`.
SCOPE_NAMES = '__rs_names__'
# Its attribute for the names of the class body trying the pattern. No scope name can be this
# one: a name starting with `__` is never looked up in the scope.
CLASS_NAMES = '__rs_class__'


class _ScopeNames:
    """``names.Point`` is the scope's ``Point``, looked up when the pattern is tried."""

    __slots__ = ('econtext',)

    def __init__(self, econtext: Any) -> None:
        self.econtext = econtext

    def __getattr__(self, name: str) -> Any:
        return self.econtext.get_name(name)

    @property
    def __rs_class__(self) -> _ClassNames:
        # The caller is the class body trying the pattern, its locals the class's namespace.
        return _ClassNames(self.econtext, sys._getframe(1).f_locals)


class _ClassNames:
    """``names.__rs_class__.Point`` is the class body's ``Point`` if it is bound by then, and
    otherwise the scope's: what a read of a class's name is anywhere else in its body."""

    __slots__ = ('econtext', 'names')

    def __init__(self, econtext: Any, names: Any) -> None:
        self.econtext = econtext
        self.names = names

    def __getattr__(self, name: str) -> Any:
        try:
            return self.names[name]
        except KeyError:
            return self.econtext.get_name(name)


MODULE_GLOBALS = {
    '__default': sys.intern('__default__'),
    '__rs_name_error': _name_error,
    '__rs_names_of': _ScopeNames,
    '__rs_globals__': globals,
    '__rs_locals__': locals,
}


def _ours(name: str) -> bool:
    """A name of the compiled function itself (parameters, globals): Chameleon's generated
    code has none of these, so an expression naming one gets a `NameError`, as there."""
    return name.startswith('__rs_') or name == '__lookup_attr'


def rebound(name: str) -> str:
    """The template scope's entry for a builtin a code block assigned (``len = 5``): never a
    Python name (`=`), so it doesn't make ``len`` a plain scope name, as a ``def`` does."""
    return f'{name}='


def _econtext() -> ast.Name:
    return ast.Name(id='econtext', ctx=ast.Load())


def _global(name: str, in_class: bool) -> ast.expr:
    """One of the generated code's own globals (`__rs_tpl`, ...). Inside a class Python mangles a
    name with two leading underscores (`_C__rs_tpl`), so there it is read from the globals:
    through `__rs_globals__`, which isn't mangled (it ends in `__`) and which a class attribute
    named `globals` doesn't hide."""
    if not in_class:
        return ast.Name(id=name, ctx=ast.Load())
    return ast.Subscript(value=_globals(), slice=ast.Constant(name), ctx=ast.Load())


def _globals() -> ast.expr:
    return ast.Call(func=ast.Name(id='__rs_globals__', ctx=ast.Load()), args=[], keywords=[])


def _tpl(name: str, in_class: bool = False) -> ast.Subscript:
    return ast.Subscript(value=_global('__rs_tpl', in_class), slice=ast.Constant(name), ctx=ast.Load())


class Rewriter(ast.NodeTransformer):
    """`NameLookupRewriteVisitor(NameTransform(...))` plus `transform_attribute`, recording
    the scope names the code reads and writes (D9).

    Like Chameleon's visitor, scopes do not chain: a nested lambda sees only its own
    parameters, and a `def` or an import adds its name to the scope it appears in for good.
    """

    def __init__(
        self,
        aliases: dict[str, str],
        template_builtins: Iterable[str] = (),
        own: Optional[Callable[[str], bool]] = None,
        local_names: Optional[set[str]] = None,
        code_block: bool = False,
        attributes: bool = True,
    ) -> None:
        # Whether `obj.name` falls back to `obj['name']` (`transform_attribute`): a part of
        # Chameleon's `PythonExpr`, not of its compiler, so not done for a custom type's
        # statements (a type that wants it applies it itself, as `PythonExpr.translate` does).
        self.attributes = attributes
        # A `<?python ?>` block: Python's builtins are called directly, as in Chameleon's
        # generated code (`econtext['y'] = len(...)`), never looked up in the scope first, so
        # render data named `len` or `str` can't replace them there (expressions do look
        # them up: `get('len', len)`).
        self.code_block = code_block
        # The outermost scope is the template's: names a code block defined (a `def`, its
        # parameters, an import) stay plain names in every expression compiled after it, as
        # Chameleon's one visitor per template keeps them.
        self.scopes: list[set[str]] = [local_names if local_names is not None else set()]
        # Whether the code reads such a name (it then runs in the render's namespace).
        self.uses_locals = False
        # Names our own generated code uses (the custom-type bridge), left alone.
        self.own = own or (lambda name: False)
        # Compile-time aliases (`default`, `attrs`): name to the parameter or global holding it.
        self.aliases = aliases
        self.template_builtins = frozenset(template_builtins)
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self.dynamic = False
        # The render-function locals the code uses (`translate`, ...).
        self.locals: set[str] = set()
        # The names bound in the body of each class being visited (see `visit_ClassDef`);
        # `None` while inside a function or lambda in it, which doesn't see them.
        self.class_names: list[Optional[set[str]]] = []
        # The names each function, lambda and class body being visited declares `global`,
        # innermost last.
        self.declared: list[set[str]] = []
        # Inside a function: the template's plain names where the outermost function around
        # the code is defined. The scope there also holds the parameters and local functions
        # of the functions around the code, which a name declared `global` is not.
        self.template_names: Optional[set[str]] = None
        # Marks of rebound builtins (see `stored`) that outlive the function they were made in.
        self.kept: set[str] = set()
        # Whether a type parameter's bound is being visited (see `type_parameters`).
        self.lazy = False
        # How many functions deep the code is, and the classes being defined outside every
        # function: their names, which the functions in them see (see `visit_ClassDef`).
        self.functions = 0
        self.defining: set[str] = set()
        # Whether a pattern of the `match` being visited reads a name from the scope.
        self.scope_names = False

    def visit_arg(self, node: ast.arg) -> ast.arg:
        self.scopes[-1].add(node.arg)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST | list[ast.stmt]:
        return self.function(node)

    # The next four bind a name through a plain string on the node, not an `ast.Name` store,
    # which Chameleon's visitor misses: every read of it became a scope lookup, a `NameError`.
    # Here the name works.

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST | list[ast.stmt]:
        return self.function(node)

    def function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST | list[ast.stmt]:
        # Chameleon binds a function's name before it visits anything else, so `@f` on
        # `def f`, or `def f(x=f)`, with `f` in the render data doesn't read it (an
        # `UnboundLocalError` there). It has no `async def` to copy: that one is Python's,
        # its name bound after its decorators, defaults and annotations are evaluated.
        # So is a function in a class body, which Chameleon can't run (its name is the
        # template's there only if the body declares it `global`).
        late = isinstance(node, ast.AsyncFunctionDef) or self.in_class_body()
        if not late:
            self.bind(node.name)
        # Decorators, defaults and annotations are evaluated where the function is defined;
        # its body is a scope of its own.
        node.decorator_list = [self.visit(decorator) for decorator in node.decorator_list]
        # Chameleon keeps a function's parameters, and what its body defines, as plain names
        # for the rest of the template (`def f(a)`, then `${a}`: a `NameError`). Not so for a
        # function inside a class, which Chameleon can't run: those names end with it.
        python = self.in_class()
        scope = self.scopes[-1]
        before = set(scope)
        outermost = self.template_names is None
        if outermost:
            self.template_names = before
        try:
            # Its type parameters are names for all of it but the decorators. Their bounds are
            # evaluated in a scope that doesn't see its parameters, so they are visited first.
            with self.plain(_type_names(node)):
                self.type_parameters(node)
                if python:
                    self.defaults(node.args)
                    self.parameters(node.args)
                else:
                    self.generic_visit(node.args)
                if node.returns is not None:
                    node.returns = self.visit(node.returns)
                if late and self.bind(node.name):
                    before.add(node.name)
                self.class_names.append(None)
                self.declared.append(_bindings(node.body)[1])
                self.functions += 1
                try:
                    # The function runs once a class being defined around it is bound.
                    with self.plain(self.defining):
                        node.body = self.statements(node.body)
                finally:
                    self.functions -= 1
                    self.declared.pop()
                    self.class_names.pop()
        finally:
            if python:
                scope.intersection_update(before | self.kept)
            if outermost:
                self.template_names = None
        return self.stored(node, [node.name])

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST | list[ast.stmt]:
        # A class body is Python's: what it binds (an assignment, an annotation, a method)
        # is the class's attribute, read as a plain name in the body itself. Chameleon stores
        # an assignment there in the template scope like any other, so `class C: v = 3` has
        # no `C.v` (and its dataclasses no fields). Functions in the body don't see those
        # names, as in Python: an assignment in a method is still a store in the scope.
        # Its decorators, bases and keywords are evaluated before its name is bound: in
        # `class Base(Base)` the base is the scope's `Base`.
        node.decorator_list = [self.visit(decorator) for decorator in node.decorator_list]
        # Its type parameters are names for the rest of it: `class Box[T](list[T])`.
        with self.plain(_type_names(node)):
            self.class_definition(node)
        return self.stored(node, [node.name])

    def class_definition(self, node: ast.ClassDef) -> None:
        node.bases = [self.visit(base) for base in node.bases]
        node.keywords = [self.visit(keyword) for keyword in node.keywords]
        # Nor is it bound while its body runs: outside a function `class Base: b = Base` reads
        # the scope's `Base` (a class's own earlier value, if a block already defined one).
        # Its methods run later and read the class. Inside a function the name is that
        # function's local, unbound in the body, as in Python.
        scope = self.scopes[-1]
        binds = not self.in_class_body() and not self.is_global(node.name)
        deferred = binds and self.functions == 0 and node.name not in scope | self.defining
        if deferred:
            self.defining.add(node.name)
        else:
            self.bind(node.name)
        try:
            self.type_parameters(node)
            names, global_names = _bindings(node.body, class_body=True)
            self.class_names.append(names - global_names)
            self.declared.append(global_names)
            try:
                node.body = self.statements(node.body)
            finally:
                self.class_names.pop()
                self.declared.pop()
        finally:
            if deferred:
                self.defining.discard(node.name)
        if deferred:
            self.bind(node.name)

    def type_parameters(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        """Visits the type parameters of ``node``. A bound is evaluated later, once a class
        being defined around it is bound, in a scope of its own that reads a class body's
        names from the finished class: those stay plain names, which Python compiles so."""
        lazy, self.lazy = self.lazy, True
        try:
            with self.plain(self.defining):
                node.type_params = [self.visit(param) for param in node.type_params]
        finally:
            self.lazy = lazy

    def bind(self, name: str) -> bool:
        """A `def`, a `class` or an import makes ``name`` a plain name of the template, and
        this returns whether it did. Not in a class body, where the name is the class's, nor
        for a name declared `global` inside a class (see `stored`)."""
        if self.in_class_body() or self.is_global(name):
            return False
        self.scopes[-1].add(name)
        return True

    def is_global(self, name: str) -> bool:
        """Whether the code is inside a class, in a function or class body that declares
        ``name`` `global`: the template's name, as in Python. (Outside a class a function's
        names are Chameleon's, which has no such rule.)"""
        return self.in_class() and bool(self.declared) and name in self.declared[-1]

    def plain_global(self, name: str) -> bool:
        """Whether ``name``, declared `global` here, is a plain name of the template."""
        return name in (self.scopes[0] if self.template_names is None else self.template_names)

    def stored(self, node: ast.stmt, names: Iterable[str]) -> ast.AST | list[ast.stmt]:
        """``node``, a `def`, a `class` or an import, followed by a store in the scope of each
        of the ``names`` it binds that is declared `global` here: where an assignment to such
        a name goes, and where the rest of the template reads it. Only once the statement
        has run, so one in a method never called, or in a branch not taken, changes nothing."""
        stores: list[ast.stmt] = []
        for name in names:
            if self.code_block and name in BUILTINS and self.is_global(name):
                # A builtin: a plain name of the render's namespace, marked for the
                # expressions after it as an assignment to one is (see `rebound`).
                self.scopes[0].add(rebound(name))
                self.kept.add(rebound(name))
            elif self.is_global(name) and not self.plain_global(name):
                self.writes.add(name)
                target = ast.Subscript(value=_econtext(), slice=ast.Constant(name), ctx=ast.Store())
                stores.append(ast.Assign(targets=[target], value=ast.Name(id=name, ctx=ast.Load())))
        return [node, *stores] if stores else node

    def imported(self, node: ast.Import | ast.ImportFrom) -> ast.AST | list[ast.stmt]:
        self.generic_visit(node)
        return self.stored(node, [alias.asname or alias.name.split('.')[0] for alias in node.names])

    visit_Import = imported
    visit_ImportFrom = imported

    def in_class_body(self) -> bool:
        return bool(self.class_names) and self.class_names[-1] is not None

    def in_class(self) -> bool:
        """Whether the code is anywhere inside a class, its methods included: where Python
        mangles names (see `_global`). A lambda or a function alone is not a class: `__rs_tpl`
        is a parameter of a compiled expression, which `globals()` doesn't have (inside a
        lambda, reading a template builtin once raised `KeyError: '__rs_tpl'`)."""
        return any(names is not None for names in self.class_names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.AST:
        # `except E as name`: a plain name inside the handler only, as Python unbinds it when
        # the handler ends (after it, the name is the scope's again). The type is evaluated
        # before the name is bound: in `except E as E` it is the scope's `E`.
        if node.type is not None:
            node.type = self.visit(node.type)
        with self.plain(filter(None, [node.name])):
            node.body = self.statements(node.body)
        return node

    def visit_match_case(self, node: ast.match_case) -> ast.AST:
        # A pattern's captures (`case [a, *rest]`, `case int() as n`) can't be stores in the
        # scope, so they are plain names in the guard and stored in the scope first thing in
        # the body: assigned names, as a `for` target or a `with ... as` is.
        names = list(dict.fromkeys(_captures(node.pattern)))
        node.pattern = self.visit(node.pattern)
        if node.guard is not None:
            with self.plain(names):
                node.guard = self.visit(node.guard)
        stores: list[ast.stmt] = []
        for name in names:
            target = self.visit_Name(ast.Name(id=name, ctx=ast.Store()))
            if isinstance(target, ast.Subscript):
                stores.append(ast.Assign(targets=[target], value=ast.Name(id=name, ctx=ast.Load())))
        node.body = stores + self.statements(node.body)
        return node

    def visit_Match(self, node: ast.Match) -> ast.AST | list[ast.stmt]:
        # A class or value in a pattern must be a dotted name, so one that is the scope's
        # (`case Point(x=a)`, `Point` from the render data) can't be the lookup every other
        # read becomes: `SyntaxError`, in Chameleon too. It is read as an attribute of an
        # object that looks names up in the scope, made just before the `match`.
        before = self.scope_names
        self.scope_names = False
        try:
            node = cast('ast.Match', self.generic_visit(node))
            used = self.scope_names
        finally:
            self.scope_names = before
        if not used:
            return node
        names = ast.Call(func=_global('__rs_names_of', self.in_class()), args=[_econtext()], keywords=[])
        target: ast.expr = ast.Name(id=SCOPE_NAMES, ctx=ast.Store())
        if self.code_block:
            # A global of the render's namespace, wherever the `match` is: an assignment
            # would make it an attribute of a class, or a local of a function that a `match`
            # in a class inside that function then reads unbound.
            target = ast.Subscript(value=_globals(), slice=ast.Constant(SCOPE_NAMES), ctx=ast.Store())
        return [ast.Assign(targets=[target], value=names), node]

    def visit_MatchClass(self, node: ast.MatchClass) -> ast.AST:
        node.cls = self.dotted(node.cls)
        node.patterns = [self.visit(pattern) for pattern in node.patterns]
        node.kwd_patterns = [self.visit(pattern) for pattern in node.kwd_patterns]
        return node

    def visit_MatchValue(self, node: ast.MatchValue) -> ast.AST:
        node.value = self.dotted(node.value) if isinstance(node.value, ast.Attribute) else self.visit(node.value)
        return node

    def dotted(self, node: ast.expr) -> ast.expr:
        """A dotted name of a pattern, its first name read from the scope if it is the scope's."""
        if isinstance(node, ast.Attribute):
            node.value = self.dotted(node.value)
            return node
        if not isinstance(node, ast.Name):
            return cast('ast.expr', self.visit(node))
        # A name the class body binds is read as any other read of it is (see `visit_Name`):
        # the class's if bound by then, and otherwise what it is outside the class.
        bound = self.class_name(node.id)
        visited = self.lookup(ast.Name(id=node.id, ctx=ast.Load())) if bound else self.visit(node)
        if isinstance(visited, ast.Name):
            return visited
        self.scope_names = True
        names: ast.expr = ast.Name(id=SCOPE_NAMES, ctx=ast.Load())
        if bound:
            names = ast.Attribute(value=names, attr=CLASS_NAMES, ctx=ast.Load())
        return ast.Attribute(value=names, attr=node.id, ctx=ast.Load())

    def statements(self, body: list[ast.stmt]) -> list[ast.stmt]:
        """``body``, each statement visited (one may become several)."""
        out: list[ast.stmt] = []
        for statement in body:
            visited = self.visit(statement)
            out.extend(visited if isinstance(visited, list) else [visited])
        return out

    @contextlib.contextmanager
    def plain(self, names: Iterable[str]) -> Generator[None]:
        """``names`` as plain names of the current scope, for the code visited inside."""
        scope = self.scopes[-1]
        added = [name for name in names if name not in scope]
        scope.update(added)
        try:
            yield
        finally:
            scope.difference_update(added)

    def visit_alias(self, node: ast.alias) -> ast.AST:
        # `import os.path` binds `os` (Chameleon records `os.path`, which is no name: reading
        # `os` afterwards was a `NameError`). In a class body the name is the class's.
        self.bind(node.asname if node.asname is not None else node.name.split('.')[0])
        return self.generic_visit(node)

    def defaults(self, args: ast.arguments) -> None:
        """Visits the defaults of ``args``: for a function inside a class, before its
        parameters are names and outside a lambda's scope, so `def f(self, a=a)` reads the
        class's or the template's `a`, as in Python. Elsewhere the order is Chameleon's
        (parameters first, so that default is the parameter itself: a `NameError`)."""
        args.defaults = [self.visit(default) for default in args.defaults]
        args.kw_defaults = [default and self.visit(default) for default in args.kw_defaults]

    def parameters(self, args: ast.arguments) -> None:
        for arg in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg):
            if arg is not None:
                self.visit(arg)

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:
        python = self.in_class()
        if python:
            self.defaults(node.args)
        # Its body is a scope of its own, which doesn't see a class's names.
        self.scopes.append(set())
        self.declared.append(set())
        try:
            if python:
                self.parameters(node.args)
            else:
                self.generic_visit(node.args)
            self.class_names.append(None)
            try:
                node.body = self.visit(node.body)
            finally:
                self.class_names.pop()
            return node
        finally:
            self.declared.pop()
            self.scopes.pop()

    def comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> ast.AST:
        # In a class body a comprehension is a scope of its own that doesn't see the class's
        # names, as a method doesn't: only its first iterable is evaluated in the class body.
        if not self.in_class_body():
            return self.generic_visit(node)
        first = node.generators[0]
        first.iter = self.visit(first.iter)
        self.class_names.append(None)
        try:
            first.target = self.visit(first.target)
            first.ifs = [self.visit(test) for test in first.ifs]
            node.generators[1:] = [self.visit(generator) for generator in node.generators[1:]]
            if isinstance(node, ast.DictComp):
                node.key = self.visit(node.key)
                node.value = self.visit(node.value)
            else:
                node.elt = self.visit(node.elt)
        finally:
            self.class_names.pop()
        return node

    visit_ListComp = comprehension
    visit_SetComp = comprehension
    visit_DictComp = comprehension
    visit_GeneratorExp = comprehension

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if not self.attributes:
            return self.generic_visit(node)
        # The object is visited; the `__lookup_attr` name is ours and stays as it is.
        return ast.Call(
            func=_global('__lookup_attr', self.in_class()),
            args=[self.visit(node.value), ast.Constant(node.attr)],
            keywords=[],
        )

    def class_name(self, name: str) -> bool:
        """Whether ``name`` is one the class body being visited binds."""
        return bool(self.class_names) and (names := self.class_names[-1]) is not None and name in names

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if not self.class_name(node.id):
            visited = self.lookup(node)
            declared = self.is_global(node.id)
            if declared and isinstance(node.ctx, ast.Del) and not isinstance(visited, ast.Name):
                # `del` of a name a class body or a method declares `global`: the template's,
                # where its assignment to it went (outside a class Chameleon can't compile one).
                return ast.Subscript(value=_econtext(), slice=ast.Constant(node.id), ctx=ast.Del())
            return visited
        if not isinstance(node.ctx, ast.Load) or self.lazy:
            return node
        # A class body reads its own name if it is bound by then, and otherwise what the name
        # is outside the class (`b = a` before `a = 1`, after `del a`, or with the assignment
        # in a branch not taken): the template's.
        outside = self.lookup(ast.Name(id=node.id, ctx=ast.Load()))
        if isinstance(outside, ast.Name):
            # A plain name there too: Python looks in the class, the globals, the builtins.
            return node
        bound = ast.Call(func=ast.Name(id='__rs_locals__', ctx=ast.Load()), args=[], keywords=[])
        test = ast.Compare(left=ast.Constant(node.id), ops=[ast.In()], comparators=[bound])
        return ast.IfExp(test=test, body=node, orelse=cast('ast.expr', outside))

    def lookup(self, node: ast.Name) -> ast.AST:
        plain = self.plain_global(node.id) if self.is_global(node.id) else node.id in self.scopes[-1]
        if plain:
            if len(self.scopes) == 1:
                self.uses_locals = True
            return node
        return self.transform(node)

    def transform(self, node: ast.Name) -> ast.AST:
        name = node.id
        if name.startswith('__'):
            # Left alone: a parameter of the render function if it is a template builtin.
            if name in self.template_builtins and isinstance(node.ctx, ast.Load):
                return _tpl(name, self.in_class())
            if _ours(name) and not self.own(name) and isinstance(node.ctx, ast.Load):
                error = _global('__rs_name_error', self.in_class())
                return ast.Call(func=error, args=[ast.Constant(name)], keywords=[])
            return node
        if name in INTERNALS:
            if isinstance(node.ctx, ast.Load):
                self.dynamic = True
            return node
        if name in DEFAULTS:
            self.locals.add(name)
            self.reads.add(DEFAULTS[name])
            return node
        if self.code_block and name in BUILTINS:
            # Loaded or bound as a plain name, a local of Chameleon's render function. Bound,
            # it is marked for the expressions after it (see `rebound`): not a scope name,
            # which a `def`, a parameter or an import makes (those stay plain names).
            if isinstance(node.ctx, ast.Store):
                self.scopes[-1].add(rebound(name))
            return node
        if isinstance(node.ctx, ast.Store):
            self.writes.add(name)
            return ast.Subscript(value=_econtext(), slice=ast.Constant(name), ctx=ast.Store())
        if name in self.aliases:
            alias = self.aliases[name]
            if alias == TARGET_LANGUAGE:
                # `i18n:target`'s `default`: `Alias(["default"], "target_language")`, an
                # ordinary name lookup.
                return self.transform(ast.Name(id='target_language', ctx=ast.Load()))
            return ast.Name(id=alias, ctx=ast.Load())
        self.reads.add(name)
        get = ast.Attribute(value=_econtext(), attr='get', ctx=ast.Load())
        if name in self.template_builtins:
            return ast.Call(func=get, args=[ast.Constant(name), _tpl(name, self.in_class())], keywords=[])
        if name in BUILTINS and rebound(name) in self.scopes[0]:
            # A builtin a code block assigned: still `get(name, name)`, its fallback that
            # render-local value (the expression runs in the code blocks' namespace).
            self.uses_locals = True
            get = ast.Attribute(value=_econtext(), attr='get', ctx=ast.Load())
            return ast.Call(func=get, args=[ast.Constant(name), node], keywords=[])
        if name in BUILTINS:
            return ast.Call(func=get, args=[ast.Constant(name), ast.Name(id=name, ctx=ast.Load())], keywords=[])
        get_name = ast.Attribute(value=_econtext(), attr='get_name', ctx=ast.Load())
        return ast.Call(func=get_name, args=[ast.Constant(name)], keywords=[])


def _type_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    """The names of the type parameters of ``node`` (`T`, `*Ts`, `**P`)."""
    kinds = (ast.TypeVar, ast.TypeVarTuple, ast.ParamSpec)
    return [param.name for param in node.type_params if isinstance(param, kinds)]


def _captures(pattern: ast.pattern) -> Iterator[str]:
    """The names a ``match`` pattern binds, in order."""
    for node in ast.walk(pattern):
        name = node.rest if isinstance(node, ast.MatchMapping) else getattr(node, 'name', None)
        if isinstance(node, (ast.MatchAs, ast.MatchStar, ast.MatchMapping)) and name is not None:
            yield name


def constant_key(value: Any) -> Any:
    """CPython's `_PyCode_ConstantKey`, for the constants `ast.parse` produces: equal values
    of the same type are one constant (`0.0` and `-0.0` are not)."""
    if isinstance(value, (float, complex)):
        return (type(value), repr(value))
    if isinstance(value, tuple):
        return (tuple, tuple(constant_key(v) for v in value))
    return (type(value), value)


# Free-threaded CPython interns (so makes immortal) every `str` constant of a code object.
INTERN_STR = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
# In a template's constant cache: its `str` constants are interned (see `constant`).
INTERN = object()


def constant(cache: dict[Any, Any], value: Any) -> Any:
    """The template's one object for this constant. Chameleon compiles a template into one
    module, whose compiler merges equal constants (a tuple's items too), so `${x is 1.5}` and
    a `1.5` elsewhere in the template are the same object.

    In a template read from a file (`INTERN` in `cache`, set on free-threaded builds) a `str`
    is interned, as CPython interns a code object's: immortal, so threads rendering the
    template share no reference count for it. Not in a template made from a string, which
    may be generated per request: its constants would never be freed."""
    if type(value) is tuple:
        value = tuple(constant(cache, v) for v in value)
    elif type(value) is str and INTERN in cache:
        value = sys.intern(value)
    return cache.setdefault(constant_key(value), value)


_NO_OPS = frozenset({'RESUME', 'NOP', 'CACHE'})


def fold(cache: dict[Any, Any], node: ast.expr) -> tuple[bool, Any]:
    """Whether the running CPython's compiler folds ``node`` (an operation on constants) into
    one constant, and that constant (from the template's cache). The code is compiled, never
    run, so ``10 ** 10 ** 10`` or ``1 / 0`` (which CPython leaves unfolded) cost nothing; the
    folding rules and size limits are therefore exactly this interpreter's."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        code = compile(ast.Expression(body=node), '<fold>', 'eval')
    ops = [i for i in dis.get_instructions(code) if i.opname not in _NO_OPS]
    if len(ops) == 2 and ops[0].opname in ('LOAD_CONST', 'LOAD_SMALL_INT') and ops[1].opname == 'RETURN_VALUE':
        return True, constant(cache, ops[0].argval)
    return False, None


class _Constants(ast.NodeTransformer):
    def __init__(self, cache: dict[Any, Any]) -> None:
        self.cache = cache

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        node.value = constant(self.cache, node.value)
        return node


TARGET_LANGUAGE = 'target_language'


def aliases_for(default_is_marker: bool | str, attrs: Optional[dict[str, str]]) -> dict[str, str]:
    """The compile-time aliases: ``default_is_marker`` is ``True`` for the marker, or
    ``'target_language'`` inside ``i18n:target``."""
    aliases = {}
    if default_is_marker == TARGET_LANGUAGE:
        aliases['default'] = TARGET_LANGUAGE
    elif default_is_marker:
        aliases['default'] = '__rs_default'
    if attrs is not None:
        aliases['attrs'] = '__rs_attrs'
    return aliases


def function(
    name: str,
    params: Iterable[str],
    body: list[ast.stmt],
    rewriter: Rewriter,
    env: dict[str, Any],
) -> Callable[..., Any]:
    """Compiles ``def name(*params): body`` (already rewritten) in a module whose globals are
    ``env``, binding the render-function locals the body uses first.

    If CPython rejects the tree (a walrus whose target became ``econtext['x']``), the code is
    unparsed and compiled from source, which raises the ``SyntaxError`` Chameleon's generated
    module raises.
    """
    bindings: list[ast.stmt] = [
        ast.Assign(
            targets=[ast.Name(id=local, ctx=ast.Store())],
            value=ast.Subscript(value=_econtext(), slice=ast.Constant(DEFAULTS[local]), ctx=ast.Load()),
        )
        for local in sorted(rewriter.locals)
    ]
    arguments = ast.arguments(
        posonlyargs=[], args=[ast.arg(arg=p) for p in params], kwonlyargs=[], kw_defaults=[], defaults=[]
    )
    definition = ast.FunctionDef(name=name, args=arguments, body=bindings + body, decorator_list=[], type_params=[])
    module = ast.Module(body=[definition], type_ignores=[])
    ast.fix_missing_locations(module)
    try:
        code = compile(module, '<template expression>', 'exec')
    except (TypeError, ValueError):
        code = compile(ast.unparse(module), '<template expression>', 'exec')
    # Like the namespace Chameleon execs its generated module in: `__builtins__` is the dict.
    namespace: dict[str, Any] = {'__builtins__': builtins.__dict__, **MODULE_GLOBALS, **env}
    exec(code, namespace)  # noqa: S102 - code built from the template's own expressions
    # Taken out of its globals: left there, the function would reach itself through
    # `__globals__`, a cycle only the collector frees once a reload drops the program.
    return namespace.pop(name)


def compile_expression(
    tree: ast.Expression,
    default_is_marker: bool | str,
    attrs: Optional[dict[str, str]],
    lookup_attr: Callable[[Any, str], Any],
    template_builtins: Iterable[str] = (),
    constants: Optional[dict[Any, Any]] = None,
    local_names: Optional[set[str]] = None,
) -> tuple[Any, list[str], list[str], bool, bool]:
    """Compile a parsed expression to ``f(econtext, rcontext, default_marker, builtins)``.

    ``default_is_marker`` and ``attrs`` are the compile-time aliases in effect where the
    expression appears (``attrs`` is the element's static attributes, or ``None`` outside
    every element); ``template_builtins`` the names of the template builtins.

    Returns the function, the scope names it reads and writes (sorted), whether it uses
    ``econtext`` or ``rcontext`` as a value (then any name may be read), and whether it
    reads a name a code block defined (``local_names``): then the first item is instead a
    code object to evaluate in the render's namespace (see `namespace`). ``constants`` is
    the template's constant cache (see `constant`).

    The function's globals hold only builtins, ``lookup_attr`` and the ``attrs`` dict, never
    the template, so a template cannot be kept alive through its own expressions (D4).
    """
    if constants is not None:
        tree = _Constants(constants).visit(tree)
    rewriter = Rewriter(aliases_for(default_is_marker, attrs), template_builtins, local_names=local_names)
    body = rewriter.visit(tree.body)
    if rewriter.uses_locals:
        expression = ast.fix_missing_locations(ast.Expression(body=body))
        try:
            code = compile(expression, '<template expression>', 'eval')
        except (TypeError, ValueError):
            code = compile(ast.unparse(expression), '<template expression>', 'eval')
        return code, sorted(rewriter.reads), sorted(rewriter.writes), True, True
    func = function(
        '__rs_expression',
        PARAMS,
        [ast.Return(value=body)],
        rewriter,
        {'__lookup_attr': lookup_attr, '__rs_attrs': attrs},
    )
    return func, sorted(rewriter.reads), sorted(rewriter.writes), rewriter.dynamic, False


def yields(node: ast.AST) -> bool:
    """Whether code makes the function it is part of a generator: a ``yield`` (or ``yield
    from``) outside nested functions, lambdas and classes (in a comprehension, only the first
    iterable is evaluated by the enclosing function). Chameleon inlines expressions and code
    blocks into a render function, which then renders nothing at all."""
    if isinstance(node, (ast.Yield, ast.YieldFrom)):
        return True
    if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return yields(node.generators[0].iter)
    return any(yields(child) for child in ast.iter_child_nodes(node))


def compile_code(
    source: str,
    local_names: set[str],
    bound_names: set[str],
    template_builtins: Iterable[str] = (),
    constants: Optional[dict[Any, Any]] = None,
) -> Any:
    """`visit_CodeBlock`: a ``<?python ?>`` block's statements (dedented, surrounding
    newlines stripped), rewritten like expressions (an assignment stores in the scope), as
    a code object run in the render's namespace (see `namespace`).

    The names the block defines (a ``def``, its parameters, an import) join ``local_names``
    for the rest of the template; ``bound_names`` collects the ones the render function
    itself binds (where Chameleon's generated code reads an unset one as an unbound local).
    """
    # Chameleon's code generator parses with an empty filename (it shows in a SyntaxError).
    tree = ast.parse(textwrap.dedent(source.strip('\n')), '')
    if yields(tree):
        # The render function is a generator: nothing runs (the caller checks `yields`).
        return None
    if constants is not None:
        tree = _Constants(constants).visit(tree)
    # `attributes=False`: `obj.name` falling back to `obj['name']` is `PythonExpr`'s, which a
    # code block doesn't go through. (It also turned `obj.name = 1` into an assignment to a
    # call, a `SyntaxError`.)
    rewriter = Rewriter({}, template_builtins, local_names=local_names, code_block=True, attributes=False)
    body = rewriter.statements(tree.body)
    bound_names.update(_bindings(body)[0])
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    try:
        return compile(module, '<template code>', 'exec')
    except (TypeError, ValueError):
        return compile(ast.unparse(module), '<template code>', 'exec')


def _bindings(body: list[ast.stmt], class_body: bool = False) -> tuple[set[str], set[str]]:
    """Names the statements bind at their own level (not inside a nested function, class,
    lambda or comprehension), and the names they declare ``global``.

    For a ``class_body``, all that makes a name the class's: also a ``match`` capture, a
    ``del``, and a walrus in what a nested function, lambda or class evaluates in the body
    (decorators, defaults, bases). A name the body declares ``global`` is not the class's."""
    names: set[str] = set()
    global_names: set[str] = set()
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.ListComp, ast.SetComp)
    nested += (ast.DictComp, ast.GeneratorExp)
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split('.')[0])
        elif class_body and isinstance(node, ast.match_case):
            names.update(_captures(node.pattern))
        elif class_body and isinstance(node, ast.Name) and isinstance(node.ctx, ast.Del):
            names.add(node.id)
        elif isinstance(node, ast.Global):
            global_names.update(node.names)
        if not isinstance(node, nested):
            stack.extend(ast.iter_child_nodes(node))
        elif class_body:
            stack.extend(_header(node))
    return names, global_names


def _header(node: ast.AST) -> list[ast.expr]:
    """What a function, a lambda or a class evaluates where it is defined."""
    out: list[ast.expr] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out += node.decorator_list
    if isinstance(node, ast.ClassDef):
        out += node.bases
        out += [keyword.value for keyword in node.keywords]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        out += node.args.defaults
        out += [default for default in node.args.kw_defaults if default is not None]
    return out


def namespace(lookup_attr: Callable[[Any, str], Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """A render's namespace for code blocks: the locals of Chameleon's render function
    (`translate`, `decode`, `on_error_handler` bound from the scope), module globals, and
    whatever the blocks define. The VM sets ``econtext``, ``rcontext`` and the template
    builtins in it before each use."""
    ns = {'__builtins__': builtins.__dict__, **MODULE_GLOBALS, '__lookup_attr': lookup_attr}
    for local, key in DEFAULTS.items():
        if key in defaults:
            ns[local] = defaults[key]
    return ns


def build(
    tree: ast.Expression,
    default_is_marker: bool | str,
    attrs: Optional[dict[str, str]],
    lookup_attr: Callable[[Any, str], Any],
) -> Callable[..., Any]:
    """The function alone (the Phase 4 interface; benchmarks load old builds that call it)."""
    return compile_expression(tree, default_is_marker, attrs, lookup_attr)[0]
