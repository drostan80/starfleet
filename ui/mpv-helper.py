#!/usr/bin/env python3
"""
Starfleet mpv helper — a tiny local HTTP daemon that launches mpv.

Run on your client machine (requires mpv in PATH):
    python3 mpv-helper.py

Then in Starfleet Settings set:
    mpv Helper URL = http://<this-machine-ip>:19450

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

# How long to wait for mpv to exit before declaring it "playing".
# A quick exit (< LAUNCH_GRACE seconds) means mpv failed (bad URL,
# auth error, missing file, etc.).
LAUNCH_GRACE = 2.0


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
        # DISPLAY / WAYLAND_DISPLAY from the helper's own environment are passed through
        env = os.environ.copy()
        _current_proc = subprocess.Popen(
            ['mpv', '--force-window=yes', url],
            env=env,
            stderr=subprocess.PIPE,
        )

        # Wait briefly: if mpv exits within LAUNCH_GRACE it means it
        # failed (bad URL, auth error, missing codec, …).  If it's still
        # alive after the grace period it's playing successfully.
        try:
            _current_proc.wait(timeout=LAUNCH_GRACE)
            # mpv exited within the grace window — report failure
            stderr_tail = ''
            try:
                stderr_tail = _current_proc.stderr.read().decode('utf-8', errors='replace')[:800]
            except Exception:
                pass
            code = _current_proc.returncode
            print(f'[mpv] FAILED  exit={code}  stderr={stderr_tail}')
            self._cors(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'mpv_failed',
                'code': code,
                'detail': stderr_tail,
            }).encode())
            return
        except subprocess.TimeoutExpired:
            # Still running after grace period — it's playing
            pass

        self._cors(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')

    def _cors(self, code):
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def log_message(self, fmt, *args):
        print(f'[http] {fmt % args}')


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Starfleet mpv helper  →  http://0.0.0.0:{PORT}')
    print('Press Ctrl-C to stop.\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopped.')
