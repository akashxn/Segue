#!/usr/bin/env python3
"""Segue: a small local app over the pipeline in core.py.

Serves one page on 127.0.0.1 and shows it in its own window. Long steps (analysing,
asking Claude, writing to rekordbox) run one at a time in a background thread and
the page polls for progress.
"""
import argparse
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import core
import loader

APP_NAME = "Segue"
VERSION = "0.1"
HERE = Path(__file__).parent
PAGE = HERE / "ui.html"
ICON = HERE / "assets" / "icon.png"
DOCK_ICON = HERE / "assets" / "icon-1024.png"
UI_STATE = core.CACHE / "ui.json"  # what the page had open, so the next launch reopens it
HOVER_SECONDS = 6


class Job:
    """The one long-running task, with a log the page can poll."""

    def __init__(self):
        self.lock = threading.Lock()
        self.kind = None
        self.playlist_id = None
        self.running = False
        self.error = None
        self.lines = []
        self.stop_requested = False
        self.started = self.ended = 0.0
        self.countdown_until = 0.0

    def log(self, message):
        self.lines.append(str(message))

    def start(self, kind, playlist_id, work):
        with self.lock:
            if self.running:
                return False
            self.kind, self.playlist_id = kind, str(playlist_id)
            self.running, self.error, self.lines, self.stop_requested = True, None, [], False
            self.started, self.countdown_until = time.time(), 0.0

        def run():
            try:
                work()
            except loader.Stopped as e:
                self.log(f"Stopped: {e}")
            except Exception as e:
                traceback.print_exc()
                self.error = str(e)
                self.log(f"Failed: {e}")
            finally:
                self.ended = time.time()
                self.running = False

        threading.Thread(target=run, daemon=True).start()
        return True

    def state(self):
        now = time.time()
        return {"kind": self.kind, "playlist_id": self.playlist_id, "running": self.running,
                "error": self.error, "lines": self.lines[-200:],
                "elapsed": round((now if self.running else self.ended) - self.started),
                "countdown": max(0.0, round(self.countdown_until - now, 1)) if self.running else 0.0}


JOB = Job()


def start_analyse(playlist_id):
    def work():
        JOB.log("Hover the mouse over the first track in rekordbox's track list.")
        JOB.countdown_until = time.time() + HOVER_SECONDS
        for _ in range(HOVER_SECONDS):
            time.sleep(1)
            if JOB.stop_requested:
                raise loader.Stopped("stopped")
        first_row = loader.mouse_position()
        JOB.log("Got it. Leave the mouse and keyboard alone; clicking another app stops the run.")
        core.analyse(playlist_id, first_row, JOB.log, lambda: JOB.stop_requested)
    return JOB.start("analyse", playlist_id, work)


def start_build(playlist_id, use_ai, fresh):
    return JOB.start("build", playlist_id, lambda: core.build_set(playlist_id, use_ai, fresh, JOB.log))


def start_write(playlist_id):
    return JOB.start("write", playlist_id, lambda: core.add_to_rekordbox(playlist_id, JOB.log))


def set_for_page(playlist_id):
    """The built set plus what the page needs to describe writing it to rekordbox."""
    built = core.load_set(playlist_id)
    if built:
        built["own_cue_tracks"] = sum(1 for t in built["tracks"] if t.get("own_cues") and not t.get("left_out"))
        built["arranged_name"] = core.arranged_name(playlist_id)
    return built


def ui_state():
    try:
        return json.loads(UI_STATE.read_text())
    except (OSError, ValueError):
        return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, content_type="application/json", status=200):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type if content_type.startswith("image/")
                         else f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, value, status=200):
        self.send(json.dumps(value), status=status)

    def do_GET(self):
        try:
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts == [""]:
                self.send(PAGE.read_bytes(), "text/html")
            elif parts == ["icon.png"]:
                self.send(ICON.read_bytes(), "image/png")
            elif parts == ["api", "playlists"]:
                self.send_json(core.list_playlists())
            elif parts == ["api", "job"]:
                self.send_json(JOB.state())
            elif parts == ["api", "state"]:
                self.send_json(ui_state())
            elif parts[:2] == ["api", "set"] and len(parts) == 3:
                self.send_json(set_for_page(parts[2]))
            elif parts[:2] == ["api", "tracks"] and len(parts) == 3:
                self.send_json(core.track_list(parts[2]))
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            parts = self.path.strip("/").split("/")
            if parts == ["api", "stop"]:
                JOB.stop_requested = True
                self.send_json({"ok": True})
                return
            if parts == ["api", "state"]:
                UI_STATE.parent.mkdir(exist_ok=True)
                UI_STATE.write_text(json.dumps(body))
                self.send_json({"ok": True})
                return
            action, playlist_id = parts[1], int(parts[2])
            if action == "analyse":
                started = start_analyse(playlist_id)
            elif action == "build":
                started = start_build(playlist_id, bool(body.get("ai", True)), bool(body.get("fresh")))
            elif action == "write":
                started = start_write(playlist_id)
            else:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json({"ok": started, "error": None if started else "Another step is still running."})
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="port to serve on (default: any free port)")
    parser.add_argument("--no-window", action="store_true", help="serve only; open the printed address yourself")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"{APP_NAME} running at {url}", flush=True)
    if args.no_window:
        server.serve_forever()
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    import webview
    import macwindow
    macwindow.set_identity(APP_NAME, VERSION)
    # The colour behind the page while it loads; matches the page so there is no flash.
    background = "#1c1c1e" if macwindow.dark_mode() else "#ffffff"
    window = webview.create_window(APP_NAME, url, width=1180, height=820, min_size=(900, 600),
                                   background_color=background)
    window.events.before_show += macwindow.style
    webview.start(icon=str(DOCK_ICON))


if __name__ == "__main__":
    main()
