#!/usr/bin/env python3
"""
Starfleet dev server — serves src/ with Cache-Control: no-store so the
browser never caches JS modules between edits.

Usage:  ./serve.sh [port]   (default 3000)
"""
import http.server
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        # Suppress 304 noise; keep everything else
        if len(args) >= 2 and args[1] == '304':
            return
        super().log_message(fmt, *args)


with http.server.HTTPServer(('', PORT), NoCacheHandler) as httpd:
    print(f'Starfleet dev server → http://localhost:{PORT}')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
