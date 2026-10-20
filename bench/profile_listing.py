"""Render the listing page in a loop so a sampling profiler can see where time goes.

Usage: venv/bin/python bench/profile_listing.py [partials|inline] [seconds]
"""

import sys
import time

from bench_render import make_videos, render, setup

template = {'partials': 'home/listing.pt', 'inline': 'home/listing_inline.pt'}[sys.argv[1]]
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

loader = setup(auto_reload=False)
videos = make_videos(500)
render(loader, template, videos=videos)  # compile outside the measured window

end = time.perf_counter() + seconds
while time.perf_counter() < end:
    render(loader, template, videos=videos)
