#!/usr/bin/env python3
# Copied from chameleon_partials (https://github.com/mikeckennedy/chameleon_partials,
# scripts/serve_docs.py), modified: serves site/ at /docs/chameleon-rs on port 8098.
"""Serve the committed site/ under the production subpath for a faithful local preview.

This mirrors the nginx ``alias`` deployment (``site/`` served at ``/docs/chameleon-rs``)
so local preview exercises the exact subpath the site is deployed under -- catching any
asset/link that would break only when hosted in a subdirectory. Port 8098, so it can run
next to the chameleon_partials preview (8099).

    venv/bin/python scripts/serve_docs.py   # -> http://127.0.0.1:8098/docs/chameleon-rs/
"""

from __future__ import annotations

import functools
import http.server
import socketserver
from pathlib import Path

PREFIX = '/docs/chameleon-rs'
PORT = 8098
ROOT = Path(__file__).resolve().parent.parent / 'site'


class H(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        c = path.split('?', 1)[0].split('#', 1)[0]
        if c.startswith(PREFIX):
            path = c[len(PREFIX) :] or '/'
        return super().translate_path(path)

    def send_head(self):
        if self.path in ('/', PREFIX):
            self.send_response(302)
            self.send_header('Location', PREFIX + '/')
            self.end_headers()
            return None
        return super().send_head()


class S(socketserver.TCPServer):
    allow_reuse_address = True


def main():
    if not ROOT.is_dir():
        raise SystemExit(f'Run build_docs.py first; {ROOT} missing')
    with S(('127.0.0.1', PORT), functools.partial(H, directory=str(ROOT))) as httpd:
        print(f'-> http://127.0.0.1:{PORT}{PREFIX}/  (Ctrl+C to stop)')
        httpd.serve_forever()


if __name__ == '__main__':
    main()
