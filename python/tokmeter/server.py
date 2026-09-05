"""Local HTTP transport. Every query reads a snapshot; scans run elsewhere."""

import errno
import ipaddress
import json
import mimetypes
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .query import aggregate

STATIC = Path(__file__).with_name("static")


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, store, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        pass

    def allowed(self):
        try:
            host = urlparse("//" + self.headers.get("Host", "")).hostname
            if host != "localhost":
                ip = ipaddress.ip_address(host)
                if not (ip.is_loopback or ip.is_private):
                    return False
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host")
        except ValueError:
            return False

    def send(self, body, mime="application/json; charset=utf-8", status=200, cache=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, data, status=200):
        self.send(json.dumps(data, separators=(",", ":"), allow_nan=False).encode(), status=status)

    def status(self):
        s = self.store.snapshot()
        return dict(
            ready=s.ready,
            generation=s.generation,
            lastRefresh=s.scanned_at,
            loadSeconds=s.load_seconds,
            scan=s.scan,
            error=s.error,
            pricingVersion=self.store.catalog.version,
            persistentCache=False,
        )

    def do_GET(self):
        if not self.allowed():
            return self.json({"error": "Local host/origin required"}, 403)
        url = urlparse(self.path)
        if url.path == "/api/status":
            return self.json(self.status())
        if url.path == "/api/aggregate":
            try:
                q = parse_qs(url.query)
                start, end = int(q["from"][0]), int(q["to"][0])

                def get(name, default):
                    return q.get(name, [default])[0]

                s = self.store.snapshot()
                if not s.ready:
                    return self.json(self.status(), 202)
                data = aggregate(
                    s,
                    start,
                    end,
                    get("granularity", "1h"),
                    get("tz", "ET"),
                    get("gaps", "fill") != "skip",
                    get("source", "cc"),
                )
                data["pricingVersion"] = self.store.catalog.version
                return self.json(data)
            except (KeyError, TypeError, ValueError) as error:
                return self.json({"error": str(error)}, 400)
        if url.path == "/api/refresh":
            return self.json({"error": "Use POST /api/refresh"}, 405)
        path = STATIC / (
            "index.html" if url.path in ("/", "/index.html") else url.path.removeprefix("/static/")
        )
        if url.path not in ("/", "/index.html") and not url.path.startswith("/static/"):
            return self.json({"error": "Not found"}, 404)
        try:
            path = path.resolve()
            if not path.is_relative_to(STATIC.resolve()) or not path.is_file():
                return self.json({"error": "Not found"}, 404)
            return self.send(
                path.read_bytes(),
                mimetypes.guess_type(path)[0] or "application/octet-stream",
                cache=path.suffix != ".html",
            )
        except OSError:
            return self.json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self.allowed():
            return self.json({"error": "Local host/origin required"}, 403)
        if self.path != "/api/refresh":
            return self.json({"error": "Not found"}, 404)
        self.store.request_refresh()
        return self.json(self.status(), 202)


def bind(store, host, port):
    for candidate in range(port, min(port + 20, 65536)):
        try:
            server = ThreadingHTTPServer((host, candidate), partial(Handler, store=store))
            server.daemon_threads = True
            return server
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    raise OSError("No free port in requested range")
