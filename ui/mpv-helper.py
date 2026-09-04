#!/usr/bin/env python3
"""
Starfleet mpv helper — a tiny local HTTP daemon that launches mpv.

Run on your client machine (requires mpv in PATH):
    python3 mpv-helper.py

Then in Starfleet Settings set:
    mpv Helper URL = http://localhost:19450

Clicking ▶ on an available episode will stream the file directly via mpv.
The Starfleet web client constructs an HTTP URL (served by nginx on your
homelab) and POSTs it here, so mpv streams the file without needing any
local mount or path mapping.
"""
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 19450

# Track the current mpv process so we can kill it before launching another.
# One episode at a time — clicking a second grab replaces the first.
_current_proc = None
_last_url = None
_last_launch_time = 0

# Minimum seconds between launches of the *same* URL (double-click guard).
DEDUPE_WINDOW = 3.0


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._cors(200)
        self.end_headers()

    def do_POST(self):
        global _current_proc, _last_url, _last_launch_time

        if self.path != '/play':
            self.send_error(404)
            return

        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            self.send_error(400, 'Invalid JSON')
            return

        url = body.get('url', '').strip()
        if not url:
            self.send_error(400, 'Missing url')
            return

        now = time.monotonic()

        # Deduplicate: same URL within DEDUPE_WINDOW seconds → ignore.
        if url == _last_url and (now - _last_launch_time) < DEDUPE_WINDOW:
            print(f'[mpv] dedupe — ignoring repeat for {url}')
            self._cors(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK (dedupe)')
            return

        # Kill the current mpv if it is still running.
        if _current_proc is not None:
            try:
                if _current_proc.poll() is None:   # still alive
                    _current_proc.terminate()
                    try:
                        _current_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        _current_proc.kill()
                    print('[mpv] previous instance terminated')
            except Exception as e:
                print(f'[mpv] could not terminate previous instance: {e}')

        print(f'[mpv] {url}')
        _last_url = url
        _last_launch_time = now

        # --force-window=yes    — always open a video window even for audio-only streams
        # --no-terminal         — don't try to attach to the terminal
        # DISPLAY / WAYLAND_DISPLAY from the helper's own environment are passed through
        env = os.environ.copy()
        _current_proc = subprocess.Popen(
            ['mpv', '--force-window=yes', '--no-terminal', url],
            env=env,
        )

        # Wait briefly to catch immediate failures (bad URL, auth 401,
        # missing codecs).  If mpv dies within ~1s the response tells
        # the client so it can show a fallback message.
        time.sleep(1.0)
        exit_code = _current_proc.poll()
        if exit_code is not None:
            print(f'[mpv] exited immediately with code {exit_code}')
            _current_proc = None
            self._cors(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'FAILED')
            return

        self._cors(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')

    def _cors(self, code):
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def log_message(self, *_):
        pass  # silence default Apache-style log; we print our own above


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Starfleet mpv helper  →  http://localhost:{PORT}')
    print('Press Ctrl-C to stop.\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopped.')
