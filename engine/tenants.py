"""
tenants.py - One private Radar per client.

Plain English: each client you add gets their own folder inside "clients" (next to the Start
button), with their own watchlist, alerts, Case Board, history and reports. The Firm Radar checks
every topic once per round, then files each result only into the folders of the clients who watch
it, so no client's data ever lands in another client's folder.

Folder layout:
    clients/
        acme-water/
            client.json             name, issues, competitors, notes
            watchlist.txt           this client's topics (edit freely)
            reports/                this client's weekly briefs
            alerts.csv, pins.json, data.json, history/   (the Firm Radar's own files)

Standard terms: multi-tenant monitoring. Each client is a "tenant"; their data is "partitioned".
"""

import csv
import json
import os
import re
import time
from datetime import datetime, timedelta

CLIENTS_DIR = os.path.join("..", "clients")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
KEEP_DAYS = 35  # history older than this is deleted to keep the folder small


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return s or "client"


def valid(slug: str) -> bool:
    return bool(SLUG_RE.match(slug or "")) and os.path.isfile(os.path.join(CLIENTS_DIR, slug, "client.json"))


def paths(slug: str) -> dict:
    """Where one client's files live."""
    if not valid(slug):
        raise ValueError("unknown client")
    d = os.path.join(CLIENTS_DIR, slug)
    info = read_info(slug)
    return {"slug": slug, "name": info.get("name", slug), "dir": d, "data": os.path.join(d, "data.json"),
            "pins": os.path.join(d, "pins.json"), "watchlist": os.path.join(d, "watchlist.txt"),
            "alerts": os.path.join(d, "alerts.csv"), "history": os.path.join(d, "history"),
            "reports": os.path.join(d, "reports"), "info": os.path.join(d, "client.json")}


def read_info(slug: str) -> dict:
    try:
        with open(os.path.join(CLIENTS_DIR, slug, "client.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def list_clients() -> list[dict]:
    out = []
    if not os.path.isdir(CLIENTS_DIR):
        return out
    for slug in sorted(os.listdir(CLIENTS_DIR)):
        if SLUG_RE.match(slug) and os.path.isfile(os.path.join(CLIENTS_DIR, slug, "client.json")):
            info = read_info(slug)
            if info.get("active", True):
                out.append(dict(info, slug=slug))
    return out


def all_tenants() -> list[dict]:
    return [paths(c["slug"]) for c in list_clients()]


def read_alerts(path: str, limit: int = 50) -> list[dict]:
    """The newest spike alerts for one client, newest first."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:][::-1]


def _clean_list(items) -> list[str]:
    if isinstance(items, str):
        items = re.split(r"[\n,;]+", items)
    out = []
    for x in items or []:
        x = " ".join(str(x).replace('"', " ").split())[:80]
        if len(x) >= 2 and x.lower() not in (o.lower() for o in out):
            out.append(x)
    return out[:25]


def topic_line(text: str) -> str:
    return f'"{text}" sourcelang:english'


def create_client(name: str, issues, competitors, notes: str = "") -> dict:
    name = " ".join(str(name).split())[:80]
    if len(name) < 2:
        raise ValueError("Give the client a name.")
    issues, competitors = _clean_list(issues), _clean_list(competitors)
    os.makedirs(CLIENTS_DIR, exist_ok=True)
    base = slug = slugify(name)
    n = 2
    while os.path.exists(os.path.join(CLIENTS_DIR, slug)):
        slug = f"{base[:36]}-{n}"
        n += 1
    d = os.path.join(CLIENTS_DIR, slug)
    os.makedirs(os.path.join(d, "reports"))
    info = {"name": name, "issues": issues, "competitors": competitors, "notes": str(notes)[:2000],
            "created": datetime.now().isoformat(timespec="minutes"), "active": True}
    with open(os.path.join(d, "client.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    lines = [f"# Watchlist for {name}  One topic per line. Lines starting with # are ignored.",
             "# Quotes = exact phrase. Two quoted parts = both must appear, e.g. \"Acme\" \"lawsuit\".",
             "", "# --- The client ---", topic_line(name)]
    if issues:
        lines += ["", "# --- Their issues ---"] + [topic_line(i) for i in issues]
    if competitors:
        lines += ["", "# --- Competitors / opponents ---"] + [topic_line(c) for c in competitors]
    with open(os.path.join(d, "watchlist.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return dict(info, slug=slug)


# ---------------- history: one small line per topic per round, used by the weekly report ----------------

def append_history(p: dict, rows: list[dict], now: float | None = None) -> None:
    now = now or time.time()
    os.makedirs(p["history"], exist_ok=True)
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    with open(os.path.join(p["history"], day + ".jsonl"), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    cutoff = (datetime.fromtimestamp(now) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for name in os.listdir(p["history"]):
        if name.endswith(".jsonl") and name[:10] < cutoff:
            try:
                os.remove(os.path.join(p["history"], name))
            except OSError:
                pass


def load_history(p: dict, since: float, until: float | None = None) -> list[dict]:
    until = until or time.time() + 60
    first = datetime.fromtimestamp(since).strftime("%Y-%m-%d")
    last = datetime.fromtimestamp(until).strftime("%Y-%m-%d")
    out = []
    if not os.path.isdir(p["history"]):
        return out
    for name in sorted(os.listdir(p["history"])):
        if not name.endswith(".jsonl") or not first <= name[:10] <= last:
            continue
        with open(os.path.join(p["history"], name), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if since <= row.get("t", 0) < until:
                    out.append(row)
    return out
