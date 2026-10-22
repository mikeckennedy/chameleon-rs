"""
The `markdown:` expression (Phase 11b): converting a ~5 KB document, against mistune with the
same GFM features, and the cost of a cache hit.

  - convert, uncached: `MarkdownProfile.convert` with the cache off (includes building `Markup`)
  - mistune: `mistune.create_markdown(plugins=[table, strikethrough, task_lists, url, footnotes])`
  - cache hit: `convert` on text already converted (the same object, and an equal copy)
  - in a template: `${markdown: body}` against `${structure: body}` on a cache hit

Build first:  make build-release
Run:          venv/bin/python bench/bench_markdown.py [--runs 7]
"""

import argparse
import time

import chameleon_rs
import mistune
from chameleon_rs import PageTemplate

SECTION = """\
## Section {n}: rendering *templates* fast

chameleon-rs compiles a template once and renders it from Rust. Here is a [link to the
docs](https://mkennedy.codes/docs/chameleon-rs/) and some `inline code`, plus **bold** and
~~struck~~ text. Autolinks work too: https://example.com/page?id={n}.

- First item with a [reference][ref{n}]
- Second item
  - Nested item with *emphasis*
- [x] A finished task
- [ ] An open task

| Engine | Render | Notes |
|---|---:|---|
| Chameleon | 2,835 ns | Python bytecode |
| chameleon-rs | 431 ns | native program |

```python
def render(page):
    return page(title='Hello', items=range({n}))
```

> A quote, with a footnote.[^{n}]

[ref{n}]: https://example.com/ref/{n}
[^{n}]: The footnote text for section {n}.

"""

DOCUMENT = ''.join(SECTION.format(n=n) for n in range(1, 8))


def timed(fn, number: int) -> float:
    start = time.perf_counter()
    for _ in range(number):
        fn()
    return (time.perf_counter() - start) / number


def best(fn, number: int, runs: int) -> float:
    return min(timed(fn, number) for _ in range(runs))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=7)
    args = parser.parse_args()
    print(f'document: {len(DOCUMENT.encode())} bytes')

    md = mistune.create_markdown(plugins=['table', 'strikethrough', 'task_lists', 'url', 'footnotes'])
    chameleon_rs.configure(markdown=True, markdown_cache=0)
    profile = chameleon_rs._settings.markdown_profiles['markdown']

    ours = best(lambda: profile.convert(DOCUMENT), 200, args.runs)
    theirs = best(lambda: md(DOCUMENT), 50, args.runs)
    print(f'convert, uncached:  {ours * 1e6:8.1f} us')
    print(f'mistune:            {theirs * 1e6:8.1f} us   ({theirs / ours:.1f}x slower)')

    chameleon_rs.configure(markdown_cache=1024)
    profile.convert(DOCUMENT)
    copy = DOCUMENT[:10] + DOCUMENT[10:]  # equal text, another object
    hit = best(lambda: profile.convert(DOCUMENT), 100_000, args.runs)
    hit_copy = best(lambda: profile.convert(copy), 100_000, args.runs)
    print(f'cache hit:          {hit * 1e9:8.0f} ns   (an equal copy: {hit_copy * 1e9:.0f} ns)')

    with_md = PageTemplate('<article>${markdown: body}</article>')
    with_structure = PageTemplate('<article>${structure: body}</article>')
    html = str(profile.convert(DOCUMENT))
    with_md(body=DOCUMENT)
    t_md = best(lambda: with_md(body=DOCUMENT), 20_000, args.runs)
    t_structure = best(lambda: with_structure(body=html), 20_000, args.runs)
    print(f'template, md hit:   {t_md * 1e9:8.0f} ns   (structure: of the HTML: {t_structure * 1e9:.0f} ns)')


if __name__ == '__main__':
    main()
