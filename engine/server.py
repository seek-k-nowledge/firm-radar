"""
server.py - The Firm Radar's private website, on your own laptop (http://localhost:8865).

Plain English: this little server lets the Firm Radar page DO things: add a client, add or remove
a client's topics, pin stories to a client's Case Board, make and save weekly briefs, and start a
check right now. It only listens on your own computer ("localhost"), so nobody else can reach it.
Every request names one client (?client=acme-water), and each client's files live in their own
folder (see tenants.py), so clients never mix.

It runs on a different address (port 8865) from your podcast Narrative Radar (port 8765), so the
two never share a screen.

Standard terms: a local HTTP server with a small JSON API (application programming interface).
"""

import json
import os
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import tenants

STATUS = "status.json"  # when the last round ran, when the next one is, which sources answered
_lock = threading.Lock()
check_now = threading.Event()  # watch.py waits on this between rounds


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def write_data(obj, path=STATUS):
    with _lock:
        _write_json(path, obj)


def client_data(slug: str) -> dict:
    """The round's status plus this one client's topics and alerts."""
    status = _read_json(STATUS, {})
    p = tenants.paths(slug)
    mine = _read_json(p["data"], None)
    if mine is None:
        return dict(status, client=slug, client_name=p["name"], topics=[], alerts=[], waiting=True)
    return dict(status, **mine)


def list_reports(p: dict) -> list[dict]:
    if not os.path.isdir(p["reports"]):
        return []
    out = [{"file": n, "time": os.path.getmtime(os.path.join(p["reports"], n))}
           for n in os.listdir(p["reports"]) if n.endswith(".html")]
    return sorted(out, key=lambda r: -r["time"])


def watch_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def add_feed(url: str, path: str) -> str:
    """Add an RSS/Atom feed (Google Alert, YouTube channel, GitHub, Bluesky profile, blog...) as a topic."""
    import sources
    url = url.strip().split()[0]
    if not re.match(r"^https?://[^\s\"]+$", url) or len(url) > 500:
        raise ValueError("That doesn't look like a feed link.")
    try:
        title, items = sources.read_feed(url)
    except Exception:
        raise ValueError("Couldn't read that link as a feed. Check it's the RSS/feed link, not the normal page.")
    title = " ".join((title or sources.feed_platform(url)).replace('"', " ").split())[:70]
    if sources.feed_platform(url) == "GitHub":
        title = "GitHub: " + (urlparse(url).path.strip("/").split("/")[0].removesuffix(".atom") or "activity")
    if sources.feed_platform(url) == "Google Alerts" and not title.lower().startswith("google alert"):
        title = "Google Alert: " + title
    line = f"feed:{url} title:{title}"
    with _lock:
        lines = watch_lines(path)
        if any(l.strip().startswith(f"feed:{url}") for l in lines):
            return line
        with open(path, "a", encoding="utf-8") as f:
            f.write(("\n" if lines and lines[-1].strip() else "") + "# feed added from the Firm Radar page\n" + line + "\n")
    return line


def add_topic(text: str, path: str) -> str:
    if text.strip().lower().startswith(("http://", "https://", "feed:")):
        return add_feed(text.strip().removeprefix("feed:"), path)
    text = " ".join(text.replace('"', " ").split())[:80]
    if len(text) < 2:
        raise ValueError("topic too short")
    line = f'"{text}" sourcelang:english'
    with _lock:
        lines = watch_lines(path)
        if any(l.strip().lower() == line.lower() for l in lines):
            return line
        with open(path, "a", encoding="utf-8") as f:
            f.write(("\n" if lines and lines[-1].strip() else "") + "# added from the Firm Radar page\n" + line + "\n")
    return line


def remove_topic(line: str, path: str) -> bool:
    with _lock:
        lines = watch_lines(path)
        keep = [l for l in lines if l.strip() != line.strip()]
        if len(keep) == len(lines):
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + "\n")
    return True


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the black window clean
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html", "/app.html"):
                with open("app.html", "rb") as fh:
                    return self._html(fh.read())
            if u.path == "/api/clients":
                return self._send(200, {"clients": tenants.list_clients(), "status": _read_json(STATUS, {})})
            if u.path == "/api/data":
                return self._send(200, client_data(qs.get("client", "")))
            if u.path == "/api/pins":
                return self._send(200, _read_json(tenants.paths(qs.get("client", ""))["pins"], []))
            if u.path == "/api/reports":
                return self._send(200, {"reports": list_reports(tenants.paths(qs.get("client", "")))})
            if u.path == "/report":
                p, name = tenants.paths(qs.get("client", "")), qs.get("file", "")
                f = os.path.join(p["reports"], name)
                if not re.match(r"^[\w.-]+\.html$", name) or not os.path.isfile(f):
                    return self._send(404, {"error": "no such report"})
                with open(f, "rb") as fh:
                    return self._html(fh.read())
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            limit = 3_000_000 if self.path == "/api/report-save" else 100_000
            n = min(int(self.headers.get("Content-Length", 0)), limit)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        try:
            if self.path == "/api/check":
                check_now.set()
                return self._send(200, {"ok": True})
            if self.path == "/api/clients":
                info = tenants.create_client(body.get("name", ""), body.get("issues", ""),
                                             body.get("competitors", ""), body.get("notes", ""))
                check_now.set()
                return self._send(200, {"client": info})
            p = tenants.paths(str(body.get("client", "")))
            if self.path == "/api/watch":
                if body.get("action") == "remove":
                    ok = remove_topic(str(body.get("line", "")), p["watchlist"])
                    check_now.set()
                    return self._send(200, {"removed": ok})
                line = add_topic(str(body.get("topic", "")), p["watchlist"])
                check_now.set()
                return self._send(200, {"added": line})
            if self.path == "/api/pins":
                pins = body.get("pins")
                if not isinstance(pins, list):
                    return self._send(400, {"error": "pins must be a list"})
                with _lock:
                    _write_json(p["pins"], pins[:500])
                return self._send(200, {"saved": len(pins)})
            if self.path == "/api/report":
                import report  # loaded here so a mistake in it can never stop the Firm Radar
                days = max(1, min(int(body.get("days", 7) or 7), 31))
                try:
                    out = report.build(p["slug"], days)
                except Exception as exc:  # report trouble should never take the Firm Radar down
                    return self._send(500, {"error": f"report failed: {exc}"})
                return self._send(200, out)
            if self.path == "/api/report-save":
                name, html = str(body.get("file", "")), body.get("html", "")
                f = os.path.join(p["reports"], name)
                if not re.match(r"^[\w.-]+\.html$", name) or not os.path.isfile(f) or not isinstance(html, str):
                    return self._send(400, {"error": "can't save that"})
                with _lock:
                    with open(f + ".tmp", "w", encoding="utf-8") as fh:
                        fh.write(html)
                    os.replace(f + ".tmp", f)
                return self._send(200, {"saved": name})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(404, {"error": "not found"})


def start(port=8865):
    """Start the server in the background. Tries the next port if one is busy. Returns the URL."""
    for p in range(port, port + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://localhost:{p}/"
    return ""
