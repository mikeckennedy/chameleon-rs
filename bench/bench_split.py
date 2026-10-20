"""
How much of a Chameleon render is Chameleon, and how much is the user's Python expressions?

For the inline listing item we time:
  (a) evaluating just the template's expressions (the irreducible Python work)
  (b) a hand-written optimal Python renderer for the same item (expressions + escaping + one join)
  (c) Chameleon rendering the same page
The gap between (a) and (c) is the engine overhead a Rust implementation can attack.
"""

import timeit

from bench_render import make_videos, render, setup

N = 500
videos = make_videos(N)


def expressions_only():
    for video in videos:
        video.id; video.id; video.title; video.title; video.id; video.author  # noqa: B018
        ' '.join([])
        '{:,}'.format(video.views)  # noqa: UP032 - the exact call the template makes


_esc = str.maketrans({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})


def handwritten():
    out = []
    a = out.append
    for v in videos:
        vid = v.id.translate(_esc)
        title = v.title.translate(_esc)
        a(f'<span class="video">\n<div>\n<a href="https://www.youtube.com/watch?v={vid}" target="_blank">\n'
          f'<img src="https://img.youtube.com/vi/{vid}/maxresdefault.jpg" class="img img-responsive {" ".join([])}" '
          f'alt="{title}" title="{title}">\n</a>\n'
          f'<a href="https://www.youtube.com/watch?v={vid}" target="_blank" class="author">{v.author.translate(_esc)}</a>\n'
          f'<div class="views">{"{:,}".format(v.views)} views</div>\n</div>\n</span>')  # noqa: UP032 - as in the template
    return ''.join(out)


loader = setup(False)


def chameleon():
    render(loader, 'home/listing_inline.pt', videos=videos)


def per_item(fn) -> float:
    n, _ = timeit.Timer(fn).autorange()
    best = min(timeit.repeat(fn, number=n, repeat=5)) / n
    return best / N


if __name__ == '__main__':
    a, b, c = per_item(expressions_only), per_item(handwritten), per_item(chameleon)
    print(f'per item, n={N}')
    print(f'  (a) expressions only            {a * 1e9:7.0f} ns   {a / c:5.1%} of Chameleon')
    print(f'  (b) hand-written Python render  {b * 1e9:7.0f} ns   {b / c:5.1%} of Chameleon')
    print(f'  (c) Chameleon                   {c * 1e9:7.0f} ns')
    print(f'  engine overhead (c - a)         {(c - a) * 1e9:7.0f} ns   {(c - a) / c:5.1%}')
