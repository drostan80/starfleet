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

Watched-status reporting (2026-09-19): when the /play request carries
episode identity (showId, season, episode) plus a token and lcarsBase,
this launches mpv with a per-launch JSON IPC socket
(--input-ipc-server), watches percent-pos over the session, and — on a
normal exit with percent-pos >= 90 at any point — POSTs addWatchEvent
straight to LCARS. Best-effort throughout: no config file, no setup
step, and any failure here (IPC connect, the POST itself) just logs and
is otherwise invisible — playback itself is never affected. A launch
that gets replaced by a newer one (single-instance policy below) never
reports watched, even if it had already crossed 90% before being
killed — matched by launch id, not just "did the process exit."
"""
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 19450

# Track the current mpv process so we can kill it before launching another.
# One episode at a time — clicking a second grab replaces the first.
_current_proc = None
_last_url = None
_last_launch_time = 0

# Bumped on every launch — a watched-status watcher thread compares its own
# captured id against this at completion time, so a launch that got replaced
# (killed by a newer /play) never reports watched, regardless of the percent
# it had reached before being terminated.
_launch_id = 0

# Minimum seconds between launches of the *same* URL (double-click guard).
DEDUPE_WINDOW = 3.0

# How long to wait for mpv to exit before declaring it "playing".
# A quick exit (< LAUNCH_GRACE seconds) means mpv failed (bad URL,
# auth error, missing file, etc.).
LAUNCH_GRACE = 2.0

# Threshold to count an episode as watched — mirrors the Android VLC
# client's own >=90% rule (ui/DESIGN.md §8 A1/A4), same threshold on
# both playback paths.
WATCHED_THRESHOLD = 90.0

# How long to retry connecting to mpv's own IPC socket after launch —
# mpv creates the socket asynchronously relative to process start, so
# the first connect attempt can legitimately race it.
IPC_CONNECT_TIMEOUT = 5.0
IPC_CONNECT_RETRY_INTERVAL = 0.1


def _watch_and_report(proc, ipc_path, launch_id, ctx):
    """Runs in a background thread for the duration of one mpv launch.
    Connects to mpv's JSON IPC socket, tracks the highest percent-pos
    seen, and on a normal exit with that peak >= WATCHED_THRESHOLD POSTs
    addWatchEvent to LCARS — best-effort, never raises out of this
    thread. `ctx` is {showId, season, episode, token, lcarsBase}."""
    max_percent = 0.0
    sock = None
    try:
        deadline = time.monotonic() + IPC_CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(ipc_path)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                sock = None
                time.sleep(IPC_CONNECT_RETRY_INTERVAL)
        if sock is None:
            print(f'[mpv-watch] could not connect to IPC socket {ipc_path} — skipping watched report')
            return

        sock.sendall(json.dumps({'command': ['observe_property', 1, 'percent-pos']}).encode() + b'\n')
        sock.settimeout(1.0)
        buf = b''
        while proc.poll() is None:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get('event') == 'property-change' and msg.get('name') == 'percent-pos':
                    data = msg.get('data')
                    if isinstance(data, (int, float)) and data > max_percent:
                        max_percent = data
    except Exception as e:
        print(f'[mpv-watch] IPC session failed: {e}')
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        try:
            os.unlink(ipc_path)
        except OSError:
            pass

    proc.wait()  # make sure it's genuinely finished, not just poll()'d once

    if launch_id != _launch_id:
        print('[mpv-watch] launch was superseded — not reporting watched status')
        return

    print(f'[mpv-watch] session ended at {max_percent:.1f}%')
    if max_percent < WATCHED_THRESHOLD:
        return

    try:
        body = json.dumps({
            'query': (
                'mutation($showId: ID!, $season: Int, $episode: Int) {'
                ' addWatchEvent(showId: $showId, season: $season, episode: $episode,'
                ' platform: "mpv-helper") { id } }'
            ),
            'variables': {
                'showId': ctx['showId'],
                'season': ctx.get('season'),
                'episode': ctx.get('episode'),
            },
        }).encode()
        req = urllib.request.Request(
            ctx['lcarsBase'].rstrip('/') + '/',
            data=body,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f"Bearer {ctx['token']}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = json.loads(resp.read())
        if resp_body.get('errors'):
            print(f'[mpv-watch] addWatchEvent rejected: {resp_body["errors"]}')
        else:
            print(f"[mpv-watch] marked watched: show={ctx['showId']} "
                  f"S{ctx.get('season')}E{ctx.get('episode')}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f'[mpv-watch] failed to report watched status: {e}')


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._cors(200)
        self.end_headers()

    def do_POST(self):
        global _current_proc, _last_url, _last_launch_time, _launch_id

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

        # Episode identity for watched-status reporting — all optional,
        # every other field is meaningless without showId, so that alone
        # gates whether this launch gets an IPC watcher at all.
        show_id = body.get('showId')
        season = body.get('season')
        episode = body.get('episode')
        token = body.get('token')
        lcars_base = body.get('lcarsBase')

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
        _launch_id += 1
        this_launch_id = _launch_id

        ipc_path = None
        args = ['mpv', '--force-window=yes']
        if show_id and token and lcars_base and hasattr(socket, 'AF_UNIX'):
            ipc_path = os.path.join(
                tempfile.gettempdir(), f'starfleet-mpv-{this_launch_id}-{os.getpid()}.sock'
            )
            args.append(f'--input-ipc-server={ipc_path}')
        args.append(url)

        # --force-window=yes    — always open a video window even for audio-only streams
        # DISPLAY / WAYLAND_DISPLAY from the helper's own environment are passed through
        env = os.environ.copy()
        _current_proc = subprocess.Popen(
            args,
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

        if ipc_path:
            watcher = threading.Thread(
                target=_watch_and_report,
                args=(_current_proc, ipc_path, this_launch_id, {
                    'showId': show_id, 'season': season, 'episode': episode,
                    'token': token, 'lcarsBase': lcars_base,
                }),
                daemon=True,
            )
            watcher.start()

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
