# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

chameleon-rs now follows Chameleon's `master` branch (`71e32cc`: 4.6.1 plus three fixes merged since), where it followed 4.6.0. Chameleon's suite at that commit, 238 tests, passes unmodified.

### Changed

Behaviour that changed in Chameleon, and so here. The first three are on Chameleon's `master` and not in a release yet (the Compatibility page lists them under "Ahead of Chameleon 4.6.1"); the rest are in 4.6.1.

- **`tal:on-error` no longer handles `RecursionError`** ([malthe/chameleon#462](https://github.com/malthe/chameleon/issues/462)). A macro or a partial that includes itself without end raises it, where the handler's fallback was rendered after as many levels as the stack allowed.
- **A slot fill is written where its slot is** ([malthe/chameleon#466](https://github.com/malthe/chameleon/issues/466)). With the `metal:define-slot` inside `i18n:translate` or `i18n:name`, the fill came out before the translated text, as in Chameleon. Three of the five slot corner cases on the Compatibility page are gone with it.
- **A default reads the name it is written with** ([malthe/chameleon#468](https://github.com/malthe/chameleon/issues/468)): `${(lambda a=a: a)()}` and `def f(a=a)` in a `<?python ?>` block raised `NameError`, as in Chameleon, and a lambda's default could not read a parameter of the lambda around it.
- **An unknown expression type is an `ExpressionError`**, not a bare `LookupError` ([malthe/chameleon#458](https://github.com/malthe/chameleon/issues/458)): `tal:content="nocall: x"`, or `${markdown: x}` without Markdown configured. It has the expression and its position, and like any expression error it is raised at render time unless the template is strict.
- **`tal:case` on the same element as `tal:switch` is a `LanguageError`** ([malthe/chameleon#461](https://github.com/malthe/chameleon/pull/461)); it was an `AssertionError`.
- **A slot fill of a Chameleon template older than that fix, rendered from a chameleon-rs template** that Chameleon itself called as a macro, is no longer moved into place. Only a site that renders both engines' templates in one page, on Chameleon 4.6.1 or older, could see it.
- **A `<meta>` charset ends at whitespace** when a template's encoding is sniffed ([malthe/chameleon#457](https://github.com/malthe/chameleon/pull/457)).

### Fixed

Two fixes to `<?python ?>` blocks from the review of our pull request to Chameleon ([malthe/chameleon#465](https://github.com/malthe/chameleon/pull/465)), made the same way there:

- **A name a code block binds with `def`, `class` or an import ends with its macro.** It stayed a plain name in every macro compiled after it, and in the main body: `${thing}` there raised `UnboundLocalError` (`NameError` in Chameleon) where the render data has a `thing`. Each macro is a function of its own, so the name is the template's there. Expressions in those macros also take the fast path again.
- **`except E as name`, for a name a `def`, a class or an import had bound**, left it unbound once the handler ran, so reading it afterwards raised `UnboundLocalError` (in Chameleon too). It is the template's name then, as after any handler, and still the earlier binding if the handler didn't run.

Fixes from a second reviewer's pass over rendering, async and `<?python ?>` blocks.

Rendering:

- **The last operand of `and` / `or` is no longer tested for truth.** `${fallback or report}` raised when `report`'s `__bool__` does (a lazy model object, an array); Python returns it as it is.
- **A template whose render function was replaced is that function when used as a macro.** pyramid_chameleon's `macro=` sets one, and `metal:use-macro` of such a template rendered the whole template instead of the one macro.
- **Non-ASCII text no longer slows compilation.** One character such as `·` between interpolations made every `${` try each candidate up to the end of the text: 800 fields took 0.8 s to compile, against 2 ms.
- **A global `tal:define` that fails to unpack defines nothing.** With `tal:on-error` around `tal:define="global (x, y) row"`, the whole row became the global `x` and `y` and showed up after the next macro call.
- **A repeated keyword argument is a `SyntaxError`**, as in Python: `${dict(x=1, x=2)}` rendered with the last value.
- **Unpacking raises the iterator's own error.** A row that raised after its second column, unpacked into two names, was reported as `ValueError: too many values to unpack`.
- **`locals()` and `vars()` without arguments** see the expression's frame, `econtext` included, as in Chameleon. After a `<?python ?>` block they, `dir()`, `eval` and `exec` also see what the block defined: `${eval('helper()')}` raised `NameError`.
- **A condition tests each operand once.** `${a if flag and b else c}` asked `flag` for its truth twice when it was false, which is slow for a lazy object and wrong for one whose answer changes.
- **A dict display evaluates its keys and values in Python's order**, so `{[]: 1, 'c': missing}` raises the `NameError` first.
- **A template's `_render` set to `None`** raises `TypeError` when the template is used as a macro, as in Chameleon; the whole template was rendered.

Async rendering:

- **Overlapping reads of a partial's awaitable argument** (`asyncio.gather` of two reads) all get its value; the second reader got `None`. If the first reader is cancelled, the others get `RuntimeError: cannot reuse already awaited coroutine`, not a cancellation of their own.
- **One coroutine passed under two keywords of a partial** (`a=v, b=v`) is awaited once; it raised `RuntimeError: cannot reuse already awaited coroutine`.
- **A coroutine argument a partial never uses is closed** once nothing holds it any more, as is one made for a call whose later keyword fails. A render that went well warned that the coroutine was never awaited. A coroutine something else still holds is left alone: one a helper kept to await later, or one from the render data, which a partial that is not found used to close.

`<?python ?>` blocks:

- **A slot fill sees what a code block defined.** With helpers or imports in a `<?python ?>` block at the top of a page and the content in a layout's `metal:fill-slot`, `${fmt(x)}` in the fill raised `UnboundLocalError`. The fill now shares the namespace of the function that defines it, as Chameleon's closure does.
- **So do the arguments of a native `render_partial` call**: `${render_partial('card.pt', v=fmt(x))}` raised `NameError: fmt`.
- **A parameter named like a template builtin** (`def f(template)`, `nothing`, `macros`) no longer breaks later reads of that builtin.
- **A comprehension in a class body** no longer stores its targets in the template scope: `values = [a for a in items]` left the render data's `a` as the last item. Nor does a comprehension inside it.
- **A class attribute or a parameter named `econtext`** no longer breaks name lookups after it (`AttributeError: 'str' object has no attribute 'get_name'`), however the class came to have it. Nor does any other binding of the name in a block: `econtext = ...`, a loop target.
- **A class body reads its own namespace first for every name**, so one put there by `locals()['label'] = ...` or a metaclass's `__prepare__` is found.
- **`nonlocal` works.** It was a `SyntaxError`, in Chameleon too, because the enclosing function's assignment was a store in the scope. That includes a name the function binds with a `match` capture, a `del` or a walrus in a comprehension.
- **`except E as name` for a name declared `global`** in a class body or a method unbinds the template's name when the handler ends; render data of that name outlived it.
- **A generator expression in a class body that runs after the class statement** reads the class by its name; it raised `NameError` or read render data of that name.
- **`except E as name` in a class body** makes `name` the class's own, so a read of it before the handler no longer picks up an enclosing parameter or type parameter.
- **`except E as a` on a function's own parameter** leaves it unbound after the handler, as in Python and Chameleon; the render data's `a` was read.
- **`global len` followed by an assignment to `len` in a method** rebinds the builtin for the rest of the template, as a `def` of it did.
- **A `match` capture is bound when its guard is false**, as in Python, and keeps a value the guard itself assigns to it.
- **A `type` statement can name itself**: `type Tree = list[Tree]` raised `NameError`, and `type Alias = Alias` read render data of that name.
- **A name a class body binds is, until then, the template's** wherever the class is: inside a generic class or a function, an enclosing type parameter, parameter or local of that name was read instead (`def f(a): class C: x = a; a = 1`), a `NameError` with `a` in the render data.
- **`global a` inside `class Box[a]`**, in the body or a method, is the template's `a`; reads, writes, `del` and a `def`, `class` or `type` of it used the type parameter.
- **`global Base` in a method of `class Base`** is the class: the read raised `NameError`, and an assignment was stored where the rest of the template didn't look.

## [0.2.59] - 2026-11-22

Fixes from seven more rounds of review of `<?python ?>` blocks: class bodies, `global`, type parameters and `type` statements now scope names as Python does.

### Fixed

Class bodies in `<?python ?>` blocks, which have had Python's scoping since 0.2.57 (Chameleon has none: reading the class there is a `NameError`):

- **A comprehension or generator expression in a class body** that read a name the class also binds raised `NameError`. It reads the template's name, as in Python; only its first iterable sees the class's.
- **A default of a `lambda` or a method in a class body** reads the class's name (`f = lambda self, y=x: y`), where a lambda's read the render data, and one named like its parameter (`def f(self, a=a)`) is no longer a `NameError`.
- **A `match` capture in a class body** is the class's attribute; it was stored in the template scope, over render data of that name.
- **A class attribute named `globals`** broke reads of `template`, `macros` and the other template builtins in that class (`TypeError`).
- **A method's parameters stayed plain names after it**, so a later read of the same name was a `NameError`: a second method's default (`def g(self, a=a)`), a class attribute (`y = a`), or `${a}` after the class. Inside a class a function's names now end with the function.
- **A name a class body binds is the template's while it isn't bound**, as in Python: read before the assignment, after a `del`, or with the assignment in a branch not taken. These were a `NameError`. A name the body declares `global` is the template's.
- **A walrus in a default, a decorator or a base class** in a class body (`def f(self, y=(k := 10))`) binds the class's name; it was a `SyntaxError`.
- **A class pattern from the render data in a class body** works with one `match` inside another, and leaves nothing on the class when the `match` is left by `break`, `continue` or an exception.
- **A `match` in a class inside a function that has a `match` of its own** raised `NameError` when both patterns named a class from the render data.
- **A pattern's class or value that the class body also binds** (`case Point(x=q): Point = q`) is the template's until the class binds it, like any other read; it was a `NameError`.
- **A `def`, a `class` or an import of a name declared `global`** in a class body, or in a method, binds the template's name once it runs, and `del` of such a name compiles. A name declared `global` is the template's even where a function around it has a parameter of that name. A builtin defined that way (`global len`, `def len()`) is the builtin again after `del`.
- **A parameter named like its `async def` method** (`async def f(self, f=1)`) no longer makes that name a `NameError` for the rest of the template.

And in any `<?python ?>` block:

- **A name is bound after the header that defines it is evaluated**: `class Base(Base)`, `except E as E` and `async def f(x=f)` read the render data's `Base`, `E` and `f`, where they raised `UnboundLocalError` or `NameError`.
- **Type parameters of a `def` or `class` are names inside it** (`class Box[T](list[T])`, `def m[T](self): return T`); reading one was a `NameError`. A bound reads a class body's names from the finished class, and doesn't see the function's parameters. A default doesn't see them (`def f[a](x=a)` reads the template's `a`), and neither does a name the class body itself binds.
- **A `type` statement** (`type Pair[T] = tuple[T, T]`) works, in a class body too; it was a `SyntaxError`, as in Chameleon.
- **A class's name is not bound while its body runs**: `class Base: b = Base` reads the render data's `Base` (or an earlier class of that name), and its methods read the class. It raised `UnboundLocalError`.

## [0.2.58] - 2026-11-19

Fixes from a review of 0.2.57: two things that release broke, one of its security fixes completed, and more of what a `<?python ?>` block can do.

### Security

- **A directory's name could still grow the decorator APIs' template cache** after 0.2.57: an absolute path to it under the default settings, and with `strict_names` every case spelling on a case-insensitive filesystem. The loader keeps no template for a name that is not a file.

### Fixed

- **A template builtin read inside a `lambda` raised `KeyError: '__rs_tpl'`** (0.2.57): `${(lambda q: q or nothing)(0)}`, or `template`, `macros` and the others, in any expression.
- **Type parameters of a `def` or `class` in a `<?python ?>` block were not rewritten** (0.2.57): a bound from the render data (`def f[T: Base]`) raised `NameError`.
- **`import a.b` in a `<?python ?>` block binds `a`**, where reading it was a `NameError` (as in Chameleon), and an import in a class body is the class's.
- **A class or value from the render data in a `match` pattern** (`case Point(x=a)`) was a `SyntaxError`, as in Chameleon.
- **A `@concurrent` call passed to a partial that is not found** was left "never awaited".
- The README and the docs name the platforms that have wheels (Linux glibc x86_64 and aarch64, macOS on Apple silicon, Windows x64); they claimed musl, Intel Mac and Windows on Arm wheels that were never published. 0.2.57 was the first release with Windows wheels.

## [0.2.57] - 2026-11-19

Fixes from a fourth review (bugs, races, deadlocks, security), and `<?python ?>` blocks that bind names the way Python does. The version's middle number goes up for the changes in behaviour listed under Changed.

### Security

- **An element with many attributes, nested deeply, could crash the process on a small thread stack.** Such an element is a dozen nodes deep in the compiled tree, and the stages after the build walked it with no stack check: 90 of them, which the build allows on a 512 KiB stack, overflowed it (SIGBUS). They raise `RecursionError` now.
- **`global_init(..., cache_init=False)` dropped `strict_names` and `strict_tags`** that a `configure()` had turned on, so names that leave the template folder (`../secret.pt`) rendered again. The legacy setup call keeps them.
- **Names built from request data could grow the decorator APIs' template cache without bound**, against what the security guide says: every spelling of a directory (`pages/./`, `pages//`) got its own entry, even with `strict_names`, and so did every missing absolute path or package resource under the default settings. A directory is kept under one spelling, and a name with no file behind it isn't kept.
- **A model key named `offload` changed how the integrations' `render_async(template_file, **model)` rendered**: the render moved to a worker thread, awaited nothing, and the template never saw the variable. The model is the template's, whatever its keys.
- **The task limit of a concurrent render did not hold inside a partial rendering as a task.** Each such partial started tasks of its own, as many as the page may, the count set by render data. A `@concurrent` call in a partial is awaited in place, as documented.

### Changed

- **A `<?python ?>` block binds the names Python binds without an assignment**: `except ... as`, `class`, `async def` and the captures of a `match`. Chameleon's name rewriting misses them, so reading one raised `NameError`, or read render data of the same name. A template that relied on that `NameError` (`${C | 7}` after `class C`) renders differently.
- **A class body in a `<?python ?>` block is Python's.** Its assignments, annotations and methods are the class's attributes, so `class C: v = 3` has `C.v` and a dataclass has its fields. Chameleon stores such an assignment in the template scope: a template that read `${v}` afterwards must read `${C.v}`. What a method assigns is still stored in the template scope, as in any `def`.
- **Attributes in a `<?python ?>` block are Python's**, as in Chameleon: `obj.name` falling back to `obj['name']` is for expressions only. A block that read a dictionary's key as an attribute now raises `AttributeError`, as it does there.
- **A template whose file does not exist is not kept by the loader** (see Security): `loader['/missing.pt']` is a new object each time, until the file exists.

### Fixed

- **Assigning to an attribute in a `<?python ?>` block was a `SyntaxError`** (`x.a = 1`, `x.a += 2`, `del x.a`), where Chameleon runs it.
- **A sub-expression of a custom expression type could not read what a `<?python ?>` block defined** (a `def`, an import): `UnboundLocalError`, where Chameleon renders.
- **Compiling a text with many interpolations was slower than quadratic**: each `${` tried every `}` after it, each candidate as long as the text. 2,000 `${a}` in one text node, attribute or `string:` expression took 6 s (54 s in Chameleon); 1,500 now take 5 ms. Where each interpolation ends is unchanged.
- **A `@concurrent` call passed to a native partial could be used only once in it**: the second use raised `RuntimeError: cannot reuse already awaited coroutine`, or rendered a `tal:on-error` fallback.
- **The error of the last `@concurrent` call a render waited for had no expression or position.**
- **A render resumed while the one task it waited for was still pending never returned** (a driver that is not an event loop's task); it raises `RuntimeError`, as a coroutine does.
- **Coroutines left "never awaited"**: the arguments of a partial call refused at the recursion limit, and a `@concurrent` helper under `structure:` or `markdown:` when the render failed before its task started.
- **A package folder on the search path (`pkg:dir`)**: a directory in it was taken for a template by the Starlette and Litestar engines (an error instead of "not found"), and on a case-insensitive filesystem every case spelling of a name compiled its own copy.
- The annotations use `Optional[X]` rather than `X | None`.

## [0.1.56] - 2026-10-29

Fixes from a third reliability, security and memory review: a crash freeing deeply nested expressions on a small thread stack, output and async fixes in the VM and partials, a bound on Markdown input, and smaller hygiene fixes.

### Security

- **Freeing a deeply nested expression could crash the process on a small thread stack.** The compile's nesting limit is measured on the thread that compiles, but a compiled template's expressions were freed, and traversed by the garbage collector, recursively on whatever thread released them: the deepest `string:${...}` nesting the compile allowed killed a 64 KiB thread (SIGBUS). Both are iterative now.
- **Markdown text has a length limit.** Converting takes a few hundred bytes of memory per character, so a megabyte of Markdown from a request could cost hundreds of megabytes. A profile's new `max_length` option (a million characters by default, `None` for no limit) refuses longer text with `ValueError`.
- **Placeholder nonces for concurrent renders come from the operating system's random source**, rather than from SipHash keys over a counter. Both were unpredictable; the new source needs no argument for it.

### Fixed

- **A value of several parts in a slot body kept the parts written before an error** when one part rendered a native partial whose own slot body had such a value: the inner value cleared the record of the outer one, so a `tal:on-error` around it kept output Chameleon never writes.
- **`tal:case` awaited a coroutine passed to `render_async` twice** (`RuntimeError: cannot reuse already awaited coroutine`) when the case did not match: the `default` comparison evaluates the expression again, as in Chameleon, and now reuses the awaited value.
- **A coroutine passed to a native partial could be used only once in it.** It is awaited at the partial's first use and keeps that value. A partial that is not found no longer leaves its arguments "never awaited".
- **`${default}` inside an `i18n:target` element raised `NameError`** when no `target_language` was passed; it is the render default (`None`), as in Chameleon.
- **Templates on filesystems that give different files one inode number rendered each other's content** (some FUSE, overlay and network mounts). A file with a single link is now also known by its resolved path.
- **The Jinja-shaped engines could serve a package folder's file for another name** when the search path mixed package folders (`pkg:dir`) and directories: the name cache skipped the package folders the loader tries first.
- **A loop length of `-2**63` from a custom `repeat` wrapped around** in release builds and wrote repeat whitespace where Chameleon writes none.
- **`throw()` into a render waiting for its concurrent calls** dropped an `Exception` and kept waiting, and `GeneratorExit` yielded again; the first is now raised once the other calls are cancelled, the second at once.
- **A key whose `__hash__` changed `rcontext` while a macro was called raised `InternalError`.**
- **The `<meta>` charset search was quadratic in the number of `<meta http-equiv=Content-Type content=` prefixes** without a `;` (600 KB took 20 s); it is linear.
- **A render error's argument listing could print a subclass's value at any length** (a `str` subclass whose `__len__` lies, an `int` subclass with a long `__str__`); every value is cut to 53 characters.
- **A failed import of the concurrency limit's or the error formatter's module** pinned the limit at 64 for good, or replaced the render's error with the import error; it is raised and retried, or the render's own error is raised.
- **Fallback expression functions referred to themselves through their globals**, so on GIL builds a reloaded template's old functions waited for the garbage collector.
- **Threads meeting one file under two spellings at once compiled it twice.**
- `_native.Program` refuses an out-of-range `surrogate_base`, and the `_testing` tokenizer helpers check the source length as every other entry does.
- `make lint` fails if `fancy-regex` (a test reference) ever becomes a dependency of the extension.

## [0.1.55] - 2026-10-28

Fixes from a security and stability review: templates on filesystems without inode numbers and expressions too deep for a worker thread's stack, plus smaller async, macro and compatibility fixes.

### Security

- **Templates on filesystems without inode numbers rendered each other's content.** Partials, sibling templates and the Jinja-shaped engines identified a file by device and inode, and filesystems without file IDs (FAT, exFAT and some network or FUSE mounts, notably on Windows) report inode 0 for every file: every template on such a volume was taken for the first one loaded. A zero inode now falls back to the file's resolved path.
- **An expression nested deeply could crash the process on a small thread stack.** The compile's nesting limit is measured on the thread that compiles, but evaluation recurses on the thread that renders: the deepest expressions it allowed (a few hundred levels of `a + a + ...`, `not not ...` or an attribute chain) overflowed a 128 to 256 KiB worker thread's stack (SIGBUS). Expressions deeper than 64 levels now check the stack left before they are evaluated and raise `RecursionError`.

### Fixed

- **Errors inside a slot fill name the expression that failed** (malthe/chameleon#447, reported
  by gelbi123; also #291, reported by Franco Pellegrini, and #259, reported by castilma). An
  error in a `metal:fill-slot` body was reported at the page's `metal:use-macro`, often after
  the macro's last expression, which had nothing to do with it: in Plone, every error in a
  view's content pointed at `context/main_template/macros/master`. The message now leads with
  the fill's failing expression, its file, line and source, followed by the `use-macro` that
  led there; the macro's unrelated expression is gone. Errors outside slot fills are reported
  as before. This changes messages Chameleon gets wrong; see "Chameleon bugs fixed" in the
  compatibility guide.
- **`render_async(offload=True)` lost the caller's context variables** on Python 3.12 to 3.14: the worker thread ran without the calling task's `contextvars` (a request's, a framework's current app, tracing spans). Offloaded renders now run in `asyncio.to_thread`, which copies them.
- **A value's `__del__` that used a finished async render failed with "coroutine already executing"**: the render's state was dropped while its awaitable was still borrowed. It is now dropped after.
- **A global changed while a macro call copied the globals back raised `InternalError`** (a value's `__del__` changing `rcontext` as the local it shadowed was replaced). It now raises Chameleon's `RuntimeError: dict mutated during update`.
- **A suspended async render's garbage-collector traversal skipped one value** (the target language of a slot body's conversion helpers), so a reference cycle through it could not be collected.
- **Render defaults could be paired with another settings' key on free-threaded Python**: they were cached in two attributes written one after the other, and are now one.
- **`ExpressionEvaluator` accepts no builtins** (Chameleon raises `ValueError`, malthe/chameleon#452), and **relative dotted names go up one module per leading dot** (malthe/chameleon#451; not reachable from templates).

## [0.1.54] - 2026-10-28

Zope and Plone: z3c.pt's and Zope 6.3's own test suites pass unmodified on `compat.install()`.
Also fixes for adopted upstream Chameleon issues, and three additions.

### Added

- **`pathlib.Path` wherever a path string works** (malthe/chameleon#342, reported by
  markkuleinio): a single `Path` as `search_path` (Chameleon raises "'PosixPath' object is not
  iterable"), `Path` entries in a search path, and `loader[Path('page.pt')]`. Any
  `os.PathLike` is accepted; a list or tuple of strings is still kept as the loader's own
  `search_path`, as in Chameleon.
- **Zope's `repeat.item.first(name)` and `repeat.item.last(name)`** (malthe/chameleon#144,
  reported by pbauer): whether the item starts or ends a run of items with the same `name`
  attribute (or key, for a mapping), for headings and rules in a sorted sequence:
  `<h2 tal:condition="repeat.item.first('category')">`. Without a name, whole items compare.
  Chameleon never implemented them (its docs suggest `itertools.groupby`). `RepeatItem` takes
  the repeated items as an optional third argument; one made with Chameleon's two arguments
  raises a `TypeError` from `first` and `last` only.
- **`chameleon_rs.template.HTML5_BOOLEAN_ATTRIBUTES`** (#11, reported by zopyx): Chameleon's
  12 boolean attributes plus HTML5's 20 (`required`, `autofocus`, `formnovalidate`, ...), the
  set z3c.pt uses for Zope and Plone. Set `boolean_attributes = HTML5_BOOLEAN_ATTRIBUTES` to
  render `required="required"` instead of `required="True"`. The default stays Chameleon's.

### Fixed

- **A valueless attribute given a value is written `name="value"`** (malthe/chameleon#429,
  reported by petschki): `<input disabled tal:attributes="disabled True">` rendered
  `<input disableddisabled>`, and the same for any valueless attribute set by `tal:attributes`
  or translated by `i18n:attributes` (`titlehello`, `altAlt`). It's now written as an added
  attribute is, `disabled="disabled"`. `default` still keeps the attribute as written
  (`checked`). This changes output that Chameleon gets wrong; see "Chameleon bugs fixed" in the
  compatibility guide.
- **`repeat` in a recursive macro** (malthe/chameleon#177, reported by marcidy): when a loop
  ended inside a loop of the same name, `repeat.item` kept the inner loop's item, so the
  outer loop's next items read its `index` and `number` (`-1` and `0` after a leaf's empty
  loop). The outer loop's item is put back when the inner loop ends. Loops one after another
  still leave the last loop's item, and a `repeat` passed in by the caller is left alone.
- **A local that shadows a global survives a macro call** (malthe/chameleon#445, reported by
  vnmabus): after every macro call, Chameleon copies all globals into the caller's scope, so
  `tal:define="foo ..."` inside an element reverted to the global `foo` after any
  `metal:use-macro` in it. Only globals the macro changed are copied now; a macro that sets
  the global still updates the caller, as before.
- **Custom expression types written for Chameleon reject a `${...}` candidate with its
  `ExpressionError`** (#8, reported by zopyx): the interpolator offers the longest candidate
  first and shortens it while the type raises `ExpressionError`, but it only recognized
  chameleon-rs' class, so a type raising `chameleon.exc.ExpressionError` (z3c.pt, Zope's
  `MappedExpr`) aborted the template, and any Plone attribute with two `${...}` failed to
  compile. Chameleon's class counts too now, when Chameleon is loaded; chameleon-rs still
  never imports it.
- **Chameleon's expression types can be subclassed, and z3c.pt runs on `compat.install()`**
  (#9, reported by zopyx): z3c.pt, the page template layer of Zope and Plone, subclasses
  `chameleon.tales.PythonExpr` and `ExistsExpr`, which failed with "'PythonExpr' object has no
  attribute 'translate'" and a `RecursionError`. `PythonExpr` now has Chameleon's `translate`,
  and `NotExpr`, `ExistsExpr`, `ImportExpr` and `StructureExpr` compile as Chameleon's do when
  called from Python; templates that name the classes themselves still compile natively.
  `PageTemplate.engine` and `expression_parser` exist as in Chameleon, `ExpressionEvaluator`
  works with a parser that has no `python` type, and `tales.test`, `SimpleEngine` and
  `SimpleCompiler` are there for doctests. z3c.pt's own suite passes unmodified and runs in
  `make test-integration`.
- **Custom expression types: `Builtin` names and attribute access as in Chameleon** (#9): a
  `Builtin('get')`, `Builtin('econtext')` or a template builtin in a type's AST now means what
  it does in Chameleon's render function (it raised `AttributeError` from Python's builtins),
  and attribute access in a type's AST no longer falls back to item lookup, which Chameleon
  only does inside `PythonExpr`.
- **A custom expression type's AST is left as the type made it** (found by Zope's suite): the
  bridge replaced `Symbol` and `Static` nodes inside the type's own objects, so a type that hands
  every expression the same nodes (Zope's untrusted `python:` shares one `Static` per builtin)
  failed on its second expression with `NameError: name '__rs_symbol_2' is not defined`. The
  bridge now works on copies, as Chameleon's code generator leaves the nodes alone.
- **Error reports use the template's `value_repr` when its builtins have no `template`** (found
  by Zope's suite): z3c.pt replaces a template's builtins, and Zope passes its own `template`, so
  the report used the default formatting instead of Zope's `value_repr = repr`.
- **A macro is not callable** (#10, reported by zopyx): Zope's path expressions call whatever
  they resolve to, so `metal:use-macro="context/@@main_template/macros/master"` called the
  macro and raised a `TypeError`, on every Plone page. As in Chameleon, `Macro` is a plain
  object again and its `include` (now a separate `MacroInclude`) renders it, with the same
  signature as before. Code that called a macro directly calls `macro.include(...)`, which
  works on both engines.

## [0.1.53] - 2026-10-27

Less shared reference counting per render on free-threaded Python.

### Changed

- **Less shared reference counting per render on free-threaded Python,** from Kitewing's
  profile of a page served over HTTP. Each of these updated a count every rendering thread
  shares:
  - `chameleon_rs.tal.RepeatDict` (one per looping partial) is now a native class. Chameleon's
    keeps the dict's bound `__getitem__` and `__setitem__`, which reference `dict`'s own slot
    descriptors; creating them from six threads ran no faster than from one. It keeps
    Chameleon's name, module, subclassing, errors and `copy.copy`.
  - The check for `repeat` and `Markup` values took a reference to each value's type on every
    attribute access and every emitted value.
  - Calling a builtin such as `len` took a reference to it (builtin functions are not
    immortal).
  - The VM cloned the running program's `Arc` at every frame switch, twice per partial call; it
    now holds each program once per render.
  - The partials' `_prepare` imported the template module on every call, a lookup in
    `sys.modules` that takes that dict's lock on free-threaded Python.

## [0.1.52] - 2026-10-27

Free-threaded scaling for pages with partials, and three bugs found on the way.

### Changed

- **Pages with partials scale across threads on free-threaded Python.** Every partial call
  copied the template's render defaults (`__translate`, `__decode`, ...) from one dict shared by
  every thread, and on free-threaded CPython reading a dict takes its lock: six threads
  rendering a page with 52 partials did 2.4× the work of one. A loop or a mention of `repeat`
  now only gives a scope a fresh `repeat`. The defaults are copied in only where a template
  can see them (it names `econtext`, `rcontext` or a default, has names only known at render
  time, or calls a macro); an error message still lists them, as Chameleon's does. Switching
  between a page's and its partials' frames no longer takes references to shared objects, and
  on free-threaded builds the string constants of templates read from files are interned, as
  CPython does for code constants (never those of a template made from a string, which may be
  one per request).
  At six threads a page with partials now scales 3.7× (37,500 renders a second where it was
  22,900), and a flat template shared by every thread 4.3×.

### Fixed

- **A partial whose code read a render default by its plain name** (`translate`, `decode`,
  `on_error_handler`, which expressions read as `econtext['__translate']` and so on) raised
  `KeyError` when it had no loop.
- **An async render of a macro recursing until `tal:on-error` caught the `RecursionError`
  rendered fewer levels than Chameleon** (936 against 991 at the default limit): it kept a fixed
  reserve of 64 frames. It now measures the stack as a sync render does, and stops where
  `render()` called in place of `await render_async(...)` would.
- **Litestar's `render_string` cache could lose an entry** when two threads compiled the same
  string at once: the second evicted the oldest template before storing the key the first had
  stored. The second now keeps the first one's template.

## [0.1.51] - 2026-10-23

Fixes from two reviews: for memory leaks, GC problems and anything that could crash or wedge
a web worker, then for the memory, time and cache lifetimes a small input could take.

### Security

- **Renders nested through Python could crash the process on Python 3.12 and 3.13.** A value
  whose `__html__`, or a helper, renders another template (a comment thread rendering its
  replies) nests as deep as the data goes. Before 3.14, CPython counts only Python frames, so
  on a worker thread with a small stack (512 KiB to 2 MiB) the native stack overflowed at 68 to
  268 levels (SIGBUS) instead of raising `RecursionError`. Every render now checks the stack
  left when it starts.
- **A deep `metal:fill-slot` body could crash a compile on a small thread stack.** Copying the
  compiled slot body recursed without a guard; it now runs on a stack of its own when needed.
- **The decorator and module APIs compiled and cached every spelling of a template name
  forever** (`./sub/p.pt`, `sub//p.pt`, an absolute path): with names built from a request,
  the cache grew without bound. They now load one template per file. The names remembered for
  that are bounded (1024 per loader), names of missing files are never remembered, and a name
  seen before costs no `stat`.
- **A small Markdown value could make a conversion take hundreds of megabytes.** Every row of a
  table gets as many cells as its header, and comrak's own limit is per table: four sparse
  tables of 700 columns and 700 one-cell rows, 22 KB of text, made two million cells (500 MB
  peak, 20 MB of HTML). Text whose tables would make more than 100,000 cells, or more cells
  than it has bytes, now converts with tables off (their rows show as paragraphs), checked
  before parsing.
- **Nested namespace declarations took memory quadratic in the depth.** Every start tag
  copied its parent's prefix map: 2,000 nested `xmlns:` declarations (45 KB of template)
  took 330 MB before the compile rejected the depth. A tag now shares its parent's map, or
  stores only what it adds, and lookups while parsing cost the same at any depth.
- **A long run of whitespace made compiling quadratic.** 64K spaces after an attribute took
  8 s: the attribute search, and the `<meta>` charset sniffing (which Chameleon shares, for a
  run anywhere in the template), each rescanned the rest of the run from every space. Both
  are linear now: 256K spaces compile in 2 ms.

### Added

- **`strict_names`** (opt-in: `configure(strict_names=True)` or `CHAMELEON_STRICT_NAMES=1`):
  the framework integrations' decorator and module APIs (`@template`, `render`, `response`,
  their async forms, `ErrorPage`'s template) refuse template names that could reach outside
  the template folder (absolute paths, `..`, backslashes, drive letters, `package:path` specs,
  NUL, surrounding whitespace) with the `ValueError("Template not found: ...")` a missing
  template raises. Off by default, as in the original libraries.
- **`CHAMELEON_CONCURRENT_LIMIT`** (default 64): one asynchronous render starts at most this
  many tasks for `@concurrent` calls and concurrent partials; further calls are awaited in
  place, so a `tal:repeat` over request-sized data can't fan out without bound.

### Fixed

- **A `tal:on-error` inside a `metal:fill-slot` kept text Chameleon drops**, when the
  macro's slot sits inside an `i18n:translate` or `i18n:name`. Chameleon writes a text node
  with its `${...}` expressions (and an interpolated attribute value) as one string, so when
  an expression fails, the text before it in that node was never written. chameleon-rs wrote
  it first: `<i>a<i>E</i>` where Chameleon gives `<i><i>E</i>`. The last of the slot-body
  known differences of this kind; the render fuzzer now generates handlers in such bodies.
- **A cancellation during an async render's cleanup was swallowed.** When a `@concurrent` call
  failed, the render cancelled the others and waited for their `finally` blocks; a
  `CancelledError` arriving then (a client disconnect, `asyncio.timeout`, `wait_for`) was
  replaced by the template's error, so a disconnect became a logged 500 and a timeout never
  fired. Cancellation now wins, with the template error as its context.
- **A failed `@concurrent` call no longer waits for slower calls before it in the page**: the
  render ends as soon as one fails, as a `TaskGroup` does.
- **A `StopIteration` raised in an async render** (`next()` on an exhausted iterator) made
  `await render_async()` return the exception's arguments as if they were the page. It is now
  a `RuntimeError`, as in any coroutine (PEP 479).
- **Rendering error messages could fail and lose the original message**: the source line was
  read with the locale's encoding, so a template file that isn't UTF-8 (or a non-UTF-8 locale)
  made `str()` of the error raise. The file is now read as UTF-8 with replacement characters,
  and a failure while formatting falls back to the original message.
- **A macro with a `<?python ?>` block called in a long `tal:repeat` was quadratic**: each
  call's namespace was kept until the end of the render and searched on every lookup (128k
  items took 1.3 s and 62 MB at 64k). Namespaces are dropped when their frame returns: 0.85
  µs per item at any size.
- Cancelled `@concurrent` calls and concurrent partials' arguments no longer leave "coroutine
  was never awaited" warnings.
- **The Litestar integration made a reference cycle per render** (the template callables were
  bound to the dict that held them), so each request and its model lived until the next
  garbage collection. The callables now get a copy of the context, as Litestar's Mako engine
  does.
- **Each distinct name a template used stayed in memory for the life of the process**:
  interned immortally, by PyO3 and by CPython's parser. Bare names and dotted paths
  (`${title}`, `${user.name}`) now compile without the parser, which is also faster, and names
  are interned with `sys.intern`, so they are freed with the last template using them on
  CPython 3.13 and later with the GIL (3.12 and free-threaded builds intern only immortally).
  This matters only for templates generated at runtime.
- **No more `PanicException`s from a dict changed while chameleon-rs reads it** (a key whose
  `__hash__` changes the scope, or another thread on a free-threaded build): the scope's copy,
  the defaults and the partials' error context read a snapshot, and the macro, slot, async
  step, tokenizer and element parser entry points turn any panic into `InternalError`. A
  `PanicException` is a `BaseException`, so `except Exception` handlers missed it.
- A render's saved error is visible to the garbage collector while its cancelled tasks are
  joined; a second awaiter of a suspended render gets `RuntimeError("coroutine is being
  awaited already")`; an error looking up a delegate's `throw` is raised, as in CPython.
- **Templates found next to a caller (sibling partials) were kept for the life of the
  process,** with the settings in their cache key (a `translate` function per template, say),
  and one more for each file a deployment replaced. They now live as long as a template that
  uses them, and the cache holds settings by value only when they are plain data (strings,
  numbers), so a setting or a template subclass that refers back to its template doesn't
  keep it either. A setting that can't be hashed (a dict such as `extra_builtins`) still
  isn't shared, so a sibling never keeps contents the dict has since changed.
- **A `@concurrent` call's own cancellation waited for every other call.** A helper raising
  `CancelledError` (or awaiting a cancelled future) ended the render only once the others
  finished, never if one hung. It now ends it at once, as a failure does.
- **Two names of one file (a hard link) stayed together after a deployment replaced one.**
  `b.pt` loaded as `a.pt`, the first name seen of its file, and rendered `a.pt`'s new content
  after `a.pt` was replaced. A remembered name is now checked to still name that file, in
  the integrations, the Jinja-shaped engines and the partials folder (with `auto_reload`).
- The README said async rendering never blocks the event loop; it never blocks it on I/O. A
  page that is all CPU work holds the loop while it renders (see "Offloading a render to a
  thread").

### Changed

- The Jinja-shaped engines' `get_template()` (Starlette, Litestar, Robyn) remembers the
  names it found when templates don't reload, as the decorator API does: a name seen before
  costs no `stat` (0.5 µs, from 2.3 µs).

## [0.1.50] - 2026-10-22

### Added

- **`strict_tags`: template errors reported where the mistake is.** Chameleon treats an
  unclosed start tag as an empty element, so a `tal:condition` missing its `</div>` hides
  nothing, and a `tal:repeat` missing its `</ul>` fails with a `NameError` on a later line.
  With `strict_tags=True` (on a template or loader, in `chameleon_rs.configure()`, or through
  `CHAMELEON_STRICT_TAGS=1`), an unclosed element carrying `tal:`, `metal:` or `i18n:`
  attributes, or a `<tal:...>`/`<metal:...>` element, is a `ParseError` pointing at its start
  tag. HTML void elements (`<input>`, `<img>`, ...) are allowed. Off by default, as in
  Chameleon; every template of two production sites compiles with it on.

## [0.1.49] - 2026-10-22

Fixes from a second, independent review of the 0.1.48 changes.

### Security

- **Render data could replace a code block's own `def`, parameter or import named like a
  builtin** (`def filter(...)` in a `<?python ?>` block, data named `filter`). A regression in
  0.1.48's code-block fix; it now matches Chameleon again.
- **A partial lookup could loop forever**, hanging the worker, when the calling template's
  path started with `//` and the configured folder's with `/` (or, on Windows, they differed in
  case). A folder configured as `//...` made every partial "not found".
- **On case-insensitive filesystems (macOS, Windows), each spelling of a name compiled and kept
  its own template** (`INDEX.pt`, `index.PT`): 3,000 spellings took 360 MB. Templates are now
  cached by file, in `render_partial` and the Jinja-style `get_template` APIs.
- **More deep nesting crashes fixed:** `chameleon_rs.parser.ElementParser`, and compiles on
  small thread stacks or with most of the stack already used, overflowed the native stack.
  Compiling now also checks the stack actually left (with the `stacker` crate).

### Fixed

- A directory name given to `render_partial` or a Jinja-style `get_template` is "not found",
  rather than an `IsADirectoryError`. `chameleon_rs.robyn.ChameleonTemplate` raises
  `TemplateNotFound` for a missing file too.
- With nothing configured, a template loaded by a `PageTemplateLoader` below a package's
  `templates` folder finds partials elsewhere in that folder again (a Pyramid layout).
- A `@concurrent` helper wrapped in `structure:` or `markdown:` runs concurrently again.

## [0.1.48] - 2026-10-22

Fixes from a security review of the whole codebase. Each one has a regression test.

### Security

- **`render_partial` could load files outside the template folder.** The search through the
  calling template's folders went up to the filesystem root, so a name with no `..` (such as
  `etc/hosts`) could render a system file, and a file planted in any parent folder could be
  compiled and run as a template. The search now stops at the root of the caller's template
  tree: the configured folder (for partials or any framework integration), a loader search
  path, or with nothing configured, a Python package's `templates` folder. Names that leave
  the configured folder (an absolute path, a `..` out of it, a `package:path` spec) are not
  found, as in chameleon_partials 0.2.1, which makes the same change.
- **`markdown:` in an attribute value allowed script injection.** Markdown's own quoted
  `href="..."` closed the surrounding attribute, so a link in user text could add an event
  handler. In an attribute value the result is now escaped like any string; element content
  is unchanged.
- **Wide Markdown tables took quadratic time**: a 64 KB table of 16,000 columns took 10 s to
  convert, blocking an event loop for that long. Table cells now render in linear time
  (0.06 s), with the same output.
- **Deeply nested templates crashed the process** (a native stack overflow, SIGSEGV) where
  Chameleon raises `RecursionError`. Compiling now raises `RecursionError` past a 1 MiB stack
  budget: at least as deep as Chameleon goes at the default recursion limit, and safe on a
  2 MiB thread.
- **Render data could replace Python builtins inside `<?python ?>` blocks** (passing
  `len=...`). Code blocks now call builtins directly, as Chameleon's generated code does.
- **`chameleon_rs.robyn.ChameleonTemplate` had no path check.** It now refuses names outside
  its folder, as Robyn's `JinjaTemplate` and the Starlette and Litestar drop-ins do.
- **One template could fill the template cache many times over**: each spelling of a name
  (`./big.pt`, `.//big.pt`) compiled and kept its own copy. Every spelling is now one cached
  template, in partials and in the Jinja-style `get_template` APIs.
- The Markdown cache counted HTML by its UTF-8 length; a Python `str` with one emoji takes 4
  bytes a character, so the cache could hold 4 times its byte limit. It now counts the
  string's real size.
- Filling the placeholders of `@concurrent` calls was quadratic in their number (160,000
  calls: 2.2 s). It is now linear (0.3 s).

### Fixed

- An object whose `__html__` attribute is `None` raised `TypeError`; it now falls back to
  `str()`, as in Chameleon.
- An unclosed comment with `${...}` ending in multi-byte text raised `InternalError`; comments
  are sliced by code point, as Chameleon does.
- In `render_async`, `${structure: coro()}` and `markdown:` of an awaitable wrote the
  coroutine's `repr` and never ran it. The value is now awaited, then converted.

### Changed

- Flask text responses name their charset (`text/html; charset=utf-8`), as Flask's own
  `render_template` responses and the other integrations do. chameleon-flask sent the bare
  type.
- The user guide has a **Security** page: where escaping stops (attribute names from data,
  unquoted attributes, CDATA, `<script>`, URLs) and which lookups accept names from requests.

## [0.1.47] - 2026-10-22

The first release of chameleon-rs: a Rust implementation of the
[Chameleon](https://github.com/malthe/chameleon) page template engine, meant as a drop-in
replacement. Documentation: <https://mkennedy.codes/docs/chameleon-rs/>.

### Added

- **The engine.** Templates compile to a native program that a Rust VM runs; Python runs only
  where a template calls into your code. Chameleon's template language (TAL, METAL, i18n,
  `${...}`, every expression type) and Python API (`PageTemplate`, `PageTemplateFile`,
  `PageTextTemplate`, `PageTextTemplateFile`, `PageTemplateLoader`, their options,
  `CHAMELEON_*` environment variables, the same exceptions and error messages) are kept.
- **One-line switch.** `chameleon_rs.compat.install()` makes `import chameleon` (and
  `chameleon_partials`, `chameleon_flask`, `fastapi_chameleon`, `chameleon_robyn`) give
  chameleon-rs. Stock Chameleon need not be installed. Shadow mode (`chameleon_rs.shadow`)
  renders a sample of production traffic with both engines and reports any difference.
- **Built-in partials.** `${render_partial('shared/card.pt', item=item)}` works in every
  template and renders natively inside the page; chameleon_partials' API keeps working.
- **`chameleon_rs.configure()`**: one process-wide call for the template folder,
  `auto_reload`, partial lookup (`partial_search`) and Markdown settings, applied on every
  call and thread-safe.
- **Async rendering.** `await template.render_async(**model)` awaits awaitable model values
  where the template uses them, without blocking the event loop (macros, slots, partials and
  `tal:on-error` work across the wait). With no awaitables the output is exactly `render()`'s.
  Opt-in: `@chameleon_rs.concurrent` helpers and `concurrent=True` partial fan-out run
  concurrently; `render_async(..., offload=True)` renders in a worker thread (experimental).
- **`markdown:` expressions** (opt-in, `configure(markdown=True)`): CommonMark plus the GitHub
  extensions, converted in Rust, with no Markdown package to install.
- **Framework integrations**, each importing its framework only when imported itself, with no
  extras to install:
  - `chameleon_rs.flask` (Flask and Quart): chameleon-flask's API.
  - `chameleon_rs.fastapi`: fastapi-chameleon's API.
  - `chameleon_rs.starlette`: `Templates`, a drop-in for Starlette's `Jinja2Templates`, and the
    decorator API.
  - `chameleon_rs.robyn`: chameleon-robyn's API.
  - `chameleon_rs.litestar`: `ChameleonEngine` for Litestar's `TemplateConfig`, and the
    decorator API.

  All of them render async views with `render_async`, and `configure(..., app=app)` fails at
  startup when a `@template` sits above its route decorator (`DecoratorOrderError`).
- **Typing.** The package is PEP 561 typed (`py.typed`, `_native.pyi`) and checks clean with
  `ty`.
- **Python and platforms.** CPython 3.12 to 3.15, including the free-threaded builds (3.14t,
  3.15t; the extension does not re-enable the GIL). Wheels for Linux (manylinux and musllinux,
  x86_64 and aarch64), macOS (arm64 and x86_64) and Windows (x64 and arm64), built with
  profile-guided optimization (musllinux and Windows arm64: where the build can make a
  profile, else plain). No runtime dependencies.

### Compatibility

- Chameleon's own test suite, vendored unmodified: 227 of the 227 tests that pass on
  Chameleon pass on chameleon-rs (the other 6 depend on the environment on both engines).
- All 122 fixture renders of that suite are byte-identical.
- Differential fuzzing of templates, partials, macros/slots/i18n and expressions finds no
  differences.
- The test suites of fastapi-chameleon, chameleon-flask, chameleon-robyn, chameleon_partials,
  litestar-chameleon and pyramid_chameleon pass unmodified on chameleon-rs. As in Chameleon, a
  macro that a template's new source no longer defines (a reload) still renders its last
  version, which pyramid_chameleon's macro renderer relies on.
- Every page of two production Flask/Quart sites renders byte-identical HTML.

### Performance

Release build without PGO, Python 3.15 on an Apple M-series Mac, both engines in the same
process and interleaved (`bench/bench_ab.py`, `bench/bench_macros.py`,
`bench/bench_skeleton.py`; method in `docs/plan.md`, "How speed is measured"). The released
wheels are profile-guided, about 10% faster again.

| Scenario | chameleon-rs | Chameleon | Speedup |
|---|---|---|---|
| Listing, per item | 431 ns | 2,835 ns | 6.6x |
| Macro call filling a slot, per item | 676 ns | 4,648 ns | 6.9x |
| App-shaped page | 69.6 µs | 301 µs | 4.3x |
| Native `render_partial` listing vs chameleon_partials, per item | 654–680 ns | 7,300 ns | 10.9x |
| Compile one template, median | 0.053 ms | 3.8 ms | 72x |

On a production Quart site (every page, PGO wheel, same data, median of 5): template
rendering 2.2x faster in total (per-page median 2.9x), whole requests 1.3x faster; under a
20-user load test on one single-threaded worker, throughput +34% and p99 latency 255 ms to
195 ms. See [Performance](https://mkennedy.codes/docs/chameleon-rs/user-guide/performance.html).

### Known differences

Listed on the [Compatibility](https://mkennedy.codes/docs/chameleon-rs/user-guide/compatibility.html#known-differences)
page: `template.source` is a dump of the compiled program, not Python; custom expression types
run through a bridge (types relying on Chameleon's compiler internals are not supported);
`CHAMELEON_CACHE` writes nothing; Chameleon's code-generation modules do not exist; a few
error-handler corner cases inside slot bodies; built-in partials also look next to the calling
template (`configure(partial_search='registered')` turns it off); awaitable model values are
awaited in async views; and a replaced `render()` makes `render_async()` render synchronously.
