#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim browse — the browser half of fuaim.

Reads an index produced by the indexer and presents it as a grid of waveform
cards. Standard library only, and no build step: this file is the CLI and the
HTTP server, and the interface it serves is ordinary files in `static/` beside it
— `index.html`, `app.css`, `app.js`, and the two icons.

    python3 browse.py                          # the committed fixture index
    python3 browse.py --index ~/Recordings/fuaim-index
    python3 browse.py DIR --port 9000 --no-open

What it does and does not do, from docs/index-format.md: it reads the index and
nothing else, except that it will serve the original bytes of a recording,
read-only, so the browser's own decoder can play it. It never runs ffmpeg and
never decodes anything itself.

    /              the interface (static/index.html)
    /static/...    its stylesheet, script and icons
    /config.json   what the server knows and the index cannot state
    /index/...     the index directory, read-only (records, images, proxies)
    /media/...     the resolved media root, read-only, with range requests

Quit with Ctrl-C, or the ✕ in the interface.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# Index versions this browser understands. Served to the page in /config.json,
# where it is the single source for the check the interface makes; a higher major
# number is refused with an explanation rather than rendered wrongly.
SUPPORTED_INDEX_VERSIONS = {1}

FIRST_PORT = 8765
RUNS_ON = "127.0.0.1"

# The directory name `fuaim scan` writes by default. Duplicated rather than
# shared: the two halves are separate programs and either may be run on its own,
# so neither may import the other.
INDEX_DIR_NAME = "fuaim-index"

# The browser's one write: star ratings, keyed by asset id, kept beside the
# index rather than in it — ratings are the viewer's, and a rescan rewrites
# record files without knowing or caring about them.
RATINGS_FILE = "ratings.json"

# resolve() follows a symlink, which is what lets `~/bin/fuaim-browse` point at
# this file and still find static/ beside the real one.
HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
STATIC_FILES = ("index.html", "app.css", "app.js", "icon.svg", "favicon.ico")

# Explicit rather than trusting the system's mime table for the file types the
# interface is made of; media falls back to MEDIA_TYPES and then mimetypes.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/vnd.microsoft.icon",
}

# What the audio the browser plays is made of. The suffix list doubles as what
# /static/ is allowed to serve.
MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp2": "audio/mpeg",
    ".ac3": "audio/ac3",
    ".amr": "audio/amr",
    ".mpeg": "audio/mpeg",
}


def safe_resolve(root: Path, relative: str) -> Path | None:
    """Resolve a request path inside root, or return None.

    The only defence against a request reaching outside the directories the
    browser is allowed to read, so it refuses anything that escapes rather than
    trying to sanitise it.
    """
    relative = unquote(relative).lstrip("/")
    if not relative or relative.startswith("../") or "/../" in relative:
        return None
    candidate = (root / relative).resolve()
    root = root.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def write_ratings(path: Path, ratings: dict) -> None:
    """The ratings file, written atomically the way records and images are: an
    interrupted write must not leave a half-written file where the next read
    would fail."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(ratings, indent=1, sort_keys=True) + "\n",
                         "utf-8")
    os.replace(temporary, path)


def build_handler(index_root: Path, media_root: Path, static_dir: Path,
                  ratings: dict, ratings_path: Path, ratings_lock: threading.Lock):
    class FuaimHandler(BaseHTTPRequestHandler):
        server_version = "fuaim-browse"

        def log_message(self, fmt, *args):
            path = self.path or ""
            status = str(args[1]) if len(args) > 1 else ""
            quiet = ("/index/audio/", "/index/wave/", "/index/spec/", "/index/proxies/", "/media/")
            if status.startswith("2") and path.startswith(quiet):
                return
            sys.stderr.write(f"fuaim browse: {self.address_string()} {fmt % args}\n")

        def do_GET(self) -> None:
            self.safely(include_body=True)

        def do_HEAD(self) -> None:
            self.safely(include_body=False)

        def do_POST(self) -> None:
            self.safely(include_body=True)

        def handle_rating(self) -> None:
            """Read one rating write, and answer with the value that was stored.

            `stars` is 0 to clear, 1 to 5 otherwise. The write is serialised
            behind a lock because the server is threaded, and goes to a temporary
            file that is renamed over the real one.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, OSError):
                self.send_json({"error": "could not read the request"}, 400)
                return
            asset_id = str(payload.get("id") or "")
            stars = payload.get("stars")
            if not asset_id or not isinstance(stars, int) or not 0 <= stars <= 5:
                self.send_json({"error": "expected { 'id': '…', 'stars': 0..5 }"}, 400)
                return
            with ratings_lock:
                if stars == 0:
                    ratings.pop(asset_id, None)
                else:
                    ratings[asset_id] = stars
                try:
                    write_ratings(ratings_path, ratings)
                except OSError:
                    self.send_json({"error": "could not save the rating"}, 500)
                    return
            self.send_json({"id": asset_id, "stars": stars})

        def safely(self, include_body: bool) -> None:
            """Serve one request, and let a client that has gone away go.

            A browser aborts media requests constantly — every seek, every time
            playback stops, and once more when the panel holding it closes. Each
            arrives here as a broken pipe part way through a range, which is the
            client's decision and not a fault in this server.
            """
            try:
                self.serve(include_body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def serve(self, include_body: bool) -> None:
            path = urlparse(self.path).path

            if self.command == "POST":
                if path == "/rating":
                    self.handle_rating()
                else:
                    self.send_error(404, "not found")
                return

            if path == "/ratings.json":
                with ratings_lock:
                    snapshot = dict(ratings)
                self.send_json({"ratings": snapshot})
                return

            if path == "/config.json":
                # Where the halves meet: the server knows the absolute media path
                # and whether it is reachable, and which index versions its own
                # interface can read. None of that belongs in the index.
                payload = json.dumps({
                    "index_root": str(index_root),
                    "media_root": str(media_root),
                    "media_root_available": media_root.is_dir(),
                    "supported_index_versions": sorted(SUPPORTED_INDEX_VERSIONS),
                }).encode("utf-8")
                self.send_bytes(payload, "application/json; charset=utf-8",
                                include_body=include_body)
                return

            if path in ("/", "/index.html"):
                self.send_static(static_dir / "index.html", include_body)
                return

            if path.startswith("/static/"):
                target = safe_resolve(static_dir, path[len("/static/"):])
                if target is None or not target.is_file() or target.suffix not in STATIC_TYPES:
                    self.send_error(404, "not found")
                    return
                self.send_static(target, include_body)
                return

            if path.rstrip("/") == "/index/audio":
                directory = index_root / "audio"
                ids = sorted(item.stem for item in directory.glob("*.json")) if directory.is_dir() else []
                self.send_bytes(json.dumps({"ids": ids}).encode("utf-8"),
                                "application/json; charset=utf-8", include_body=include_body)
                return

            if path.startswith("/index/"):
                target = safe_resolve(index_root, path[len("/index/"):])
                if target is None or not target.is_file():
                    self.send_error(404, "not in the index")
                    return
                # Records are served uncached so a re-index shows up on reload;
                # images and proxies are immutable in practice and cache badly if not.
                cache = "no-store" if target.suffix == ".json" else "public, max-age=3600"
                self.send_bytes(target.read_bytes(), self.guess_type(target), cache, include_body)
                return

            if path.startswith("/media/"):
                target = safe_resolve(media_root, path[len("/media/"):])
                if target is None or not target.is_file():
                    # Expected whenever the library is on an unmounted disk; the
                    # interface turns this into "copy the path" rather than a
                    # broken player.
                    self.send_error(404, "media file is not reachable")
                    return
                self.send_file_range(target, include_body)
                return

            self.send_error(404, "not found")

        def guess_type(self, target: Path) -> str:
            if target.suffix in STATIC_TYPES:
                return STATIC_TYPES[target.suffix]
            if target.suffix in MEDIA_TYPES:
                return MEDIA_TYPES[target.suffix]
            guessed, _ = mimetypes.guess_type(target.name)
            return guessed or "application/octet-stream"

        def send_static(self, target: Path, include_body: bool = True) -> None:
            """Serve one of the interface files, always uncached.

            Read from disk per request on purpose: editing the CSS or the script
            then needs only a page reload.
            """
            if not target.is_file():
                self.send_error(500, f"missing front-end file: {target.name}")
                return
            self.send_bytes(target.read_bytes(), self.guess_type(target), "no-store", include_body)

        def send_bytes(self, payload: bytes, content_type: str, cache: str = "no-store",
                       include_body: bool = True) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if include_body:
                self.wfile.write(payload)

        def send_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_file_range(self, target: Path, include_body: bool = True) -> None:
            """Serve a media file, honouring a single range request.

            Range support is why playback can be seeked at all; without it the
            browser downloads from the start every time the slider moves.
            """
            size = target.stat().st_size
            start, end = 0, size - 1
            range_header = self.headers.get("Range")
            status = 200
            if range_header:
                match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
                if match:
                    first, last = match.group(1), match.group(2)
                    if first:
                        start = int(first)
                        end = int(last) if last else size - 1
                    elif last:  # a suffix range: the last N bytes
                        start = max(0, size - int(last))
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    status = 206

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", self.guess_type(target))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            if not include_body:
                return
            with target.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    return FuaimHandler


def create_server(index_root: Path, media_root: Path, static_dir: Path, port: int,
                  ratings: dict, ratings_path: Path, ratings_lock: threading.Lock) -> ThreadingHTTPServer:
    """Bind the first free port at or above the one asked for."""
    handler = build_handler(index_root, media_root, static_dir, ratings, ratings_path,
                            ratings_lock)
    last_error: OSError | None = None
    for candidate in range(port, port + 20):
        try:
            return ThreadingHTTPServer((RUNS_ON, candidate), handler)
        except OSError as error:
            last_error = error
    raise SystemExit(f"no free port in {port}-{port + 19}: {last_error}")


def resolve_media_root(index_root: Path, manifest_path: Path, override: str | None) -> Path:
    """Where the original media lives.

    `media_root` in the manifest is normally absolute, but may be relative — the
    committed fixture uses `../media` so it survives being cloned anywhere — and a
    relative root is resolved against the index directory.
    """
    if override:
        return Path(override).expanduser().resolve()
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError):
        return index_root
    root = manifest.get("media_root")
    if not root:
        return index_root
    candidate = Path(root).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (index_root / candidate).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Browse a fuaim index as a library of waveforms.",
        epilog="The index is read-only; the browser's only write is the star "
               "ratings, kept in ratings.json beside the index. It never decodes "
               "media. See docs/index-format.md for the format it expects.",
    )
    parser.add_argument("directory", nargs="?", default=None,
                        help="library root or index directory "
                             f"(default: the committed fixture, fixtures/{INDEX_DIR_NAME})")
    parser.add_argument("--index", default=None,
                        help="the same as the positional directory, for callers that prefer a flag")
    parser.add_argument("--media-root", default=None,
                        help="override the media root recorded in the manifest")
    parser.add_argument("--port", type=int, default=FIRST_PORT,
                        help=f"first port to try (default: {FIRST_PORT}, then upwards)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser window")
    args = parser.parse_args()

    # The browser half is a directory now, not a lone file, and a half-copied one
    # should say so rather than serving a page with no stylesheet or no script.
    missing = [name for name in STATIC_FILES if not (STATIC_DIR / name).is_file()]
    if missing:
        sys.stderr.write(
            "the front end is incomplete: " + ", ".join(missing) + "\n"
            f"expected in {STATIC_DIR}\n")
        return 1

    given = args.index or args.directory
    index_root = Path(given).expanduser().resolve() if given \
        else (HERE / "fixtures" / INDEX_DIR_NAME)
    # A library root is accepted as well as an index directory, so the same path
    # can be handed to both halves: `fuaim scan DIR` writes DIR/fuaim-index, and
    # `fuaim browse DIR` reads it back without the name being typed twice.
    if not (index_root / "manifest.json").is_file() \
            and (index_root / INDEX_DIR_NAME / "manifest.json").is_file():
        index_root = index_root / INDEX_DIR_NAME
    if not (index_root / "manifest.json").is_file():
        sys.stderr.write(
            f"no index at {index_root}\n"
            f"  index a library:  fuaim scan {given or 'DIR'}\n"
            "  or point straight at an index directory with --index\n")
        return 1

    media_root = resolve_media_root(index_root, index_root / "manifest.json", args.media_root)

    # Star ratings live beside the index, not in it: they are the viewer's, and
    # a rescan rewrites record files without knowing or caring about them.
    ratings_path = index_root / RATINGS_FILE
    ratings = {}
    try:
        loaded = json.loads(ratings_path.read_text("utf-8"))
        if isinstance(loaded, dict):
            ratings = {str(key): int(value) for key, value in loaded.items()
                       if isinstance(value, (int, float)) and 1 <= int(value) <= 5}
    except (OSError, ValueError):
        ratings = {}

    server = create_server(index_root, media_root, STATIC_DIR, args.port,
                           ratings, ratings_path, threading.Lock())
    port = server.server_address[1]
    url = f"http://{RUNS_ON}:{port}/"
    print(f"fuaim browse: index {index_root}")
    print(f"fuaim browse: media {media_root}"
          f"{'' if media_root.is_dir() else '  (not found — waveforms and paths only)'}")
    print(f"fuaim browse: {url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nfuaim browse: stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
