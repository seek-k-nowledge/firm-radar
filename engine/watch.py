"""
watch.py - The Firm Radar engine: client monitoring, kept apart from your podcast Radar.

Start it with "Start Firm Radar.bat", then use the Firm Radar page at http://localhost:8865
(it opens by itself). Your podcast Narrative Radar runs separately at http://localhost:8765.

Plain English: every 15 minutes this checks every client's topics across several free sources
(GDELT, Google News, Bing News, Hacker News, Mastodon, Bluesky, Reddit, plus outlet and YouTube
feeds). Each topic is checked once per round, even if two clients watch it, and the result is
filed only into the folders of the clients who watch it. For each topic it also runs a "crowd
check" (cib.py): does the spread look like real people, or a staged crowd? If the last hour is
far above normal, it alerts you and logs it to that client's alerts.csv. Every round is saved to
the client's history, which the weekly brief (report.py) is written from.

Standard terms: multi-tenant real-time monitoring with anomaly detection (z-score) and
coordinated inauthentic behavior (CIB) detection.

Usage (from this folder):
    python watch.py                      # watch every client, forever
    python watch.py --once               # check once and stop (good for testing)
    python watch.py --every 10 --z 2.5   # check every 10 minutes, alert more easily

Optional pop-up notifications on Windows:  python -m pip install plyer
"""

import argparse
import csv
import json
import math
import os
import pathlib
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

import cib
import feeds
import server
import mood
import sources
import tenants

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
PAUSE = 15  # GDELT asks for at most one request every 5 seconds; 15 keeps us well under
MIX_V = 2  # bump when the set of sources changes, so each topic relearns its "normal" instead of crying wolf
CACHE = "radar_cache.json"  # last good reading per topic, so the dashboard always has something to show


class GdeltBusy(RuntimeError):
    """GDELT kept saying "too many requests" even after waiting."""


def open_with_retry(req, tries: int = 2) -> str:
    """Ask GDELT, and if it says "too many requests" (HTTP 429), wait 30s and try once more.
    If it still says no, give up for this round instead of hammering it (hammering keeps you blocked).
    Standard terms: retry with backoff, and a circuit breaker."""
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            if attempt == tries - 1:
                raise GdeltBusy("GDELT is busy (too many requests)")
            print("    GDELT says slow down; waiting 30s and trying once more...")
            time.sleep(30)


def gdelt(params: dict) -> dict:
    url = GDELT_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "narrative-tracker-watch/0.1 (learning project)"})
    body = open_with_retry(req)
    if not body.startswith("{"):
        raise RuntimeError(body[:200] or "empty reply")
    return json.loads(body)


def volume_series(query: str) -> list[tuple[str, float]]:
    """Article counts in 15-minute steps over the last 48 hours: [(timestamp, count), ...]."""
    data = gdelt({"query": query, "mode": "TimelineVolRaw", "format": "json", "timespan": "48h"})
    return parse_timeline(data)


def parse_timeline(data: dict) -> list[tuple[str, float]]:
    series = (data.get("timeline") or [{}])[0].get("data") or []
    return [(p.get("date", ""), float(p.get("value", 0) or 0)) for p in series]


def hourly(points: list[tuple[str, float]], per_hour: int = 4) -> list[float]:
    """Add 15-minute counts into hourly totals, newest hour last."""
    values = [v for _, v in points]
    usable = len(values) - len(values) % per_hour
    values = values[len(values) - usable:]
    return [sum(values[i:i + per_hour]) for i in range(0, usable, per_hour)]


def spike_check(hours: list[float], z_cut: float, min_articles: int) -> tuple[bool, float, float, float]:
    """Compare the newest hour with all the hours before it. Returns (is_spike, z, latest, average)."""
    if len(hours) < 8:
        return False, 0.0, hours[-1] if hours else 0.0, 0.0
    latest, history = hours[-1], hours[:-1]
    mean = statistics.mean(history)
    spread = statistics.pstdev(history) or 1.0  # avoid dividing by zero on very quiet topics
    z = (latest - mean) / spread
    return (z >= z_cut and latest >= min_articles), z, latest, mean


def notify(title: str, message: str) -> None:
    print("\a", end="")  # terminal beep
    try:
        from plyer import notification  # optional
        notification.notify(title=title, message=message[:250], timeout=15)
    except Exception:
        pass


def log_alert(row: dict, path: str) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["time", "query", "z", "last_hour", "normal_hour", "top_headline", "url"])
        if new:
            w.writeheader()
        w.writerow(row)


def load_watchlist(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except OSError:
        return []


def load_cache() -> dict:
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def last_hour_mix(items: list[dict], now: float) -> tuple[list[float], float]:
    """Hourly counts over 48h (for the chart) and how many items across all sources landed in the last hour."""
    bins = [0.0] * 48
    for it in items:
        age_h = (now - it["ts"]) / 3600
        if 0 <= age_h < 48:
            bins[47 - int(age_h)] += 1
    return bins, bins[-1]


def history_signal(hist: list, latest: float, now: float) -> tuple[float, float, bool]:
    """Search feeds don't give a fair sample over time (they favour fresh stories), so the radar
    compares each reading with its OWN earlier readings of the same thing, taken every round.
    Needs about 2.5 hours of running (6 readings older than an hour) before it will call a spike from this signal.
    Standard term: a rolling baseline built from the tool's own measurements.
    Returns (normal, z, enough_history)."""
    past = [c for t, c in hist if now - t > 3600]
    if len(past) < 6:
        return (statistics.mean(past) if past else 0.0), 0.0, False
    mean = statistics.mean(past)
    spread = max(statistics.pstdev(past), math.sqrt(max(mean, 1.0)))
    return mean, (latest - mean) / spread, True


def summarize(items: list[dict], now: float) -> dict:
    """Per-platform counts for the last 24h, who had it first, mood, and a mixed headline list."""
    day = [it for it in items if now - it["ts"] <= 86400]
    counts, first = {}, {}
    for it in day:
        p = it["platform"]
        counts[p] = counts.get(p, 0) + 1
        if p not in first or it["ts"] < first[p]:
            first[p] = it["ts"]
    heads, per = [], {}
    for it in items:
        if per.get(it["platform"], 0) < 2:
            heads.append({"title": it["title"], "url": it["url"], "domain": it["source"],
                          "platform": it["platform"], "ts": it["ts"], "image": it.get("image", "")})
            per[it["platform"]] = per.get(it["platform"], 0) + 1
        if len(heads) >= 6:
            break
    return {"counts": counts, "first": sorted(first.items(), key=lambda kv: kv[1]),
            "tone": mood.average([it["title"] for it in day]), "headlines": heads}


def check_all(queries: list[str], z_cut: float, min_articles: int, cooldown: dict, cooldown_min: int,
              use_gdelt: bool = True, cache: dict | None = None, pool: list | None = None) -> list[dict]:
    cache = {} if cache is None else cache
    stamp = datetime.now().strftime("%I:%M %p")
    print(f"\n[{stamp}] checking {len(queries)} topics across {len(sources.SOURCES) + 1} sources...")
    results, gdelt_busy = [], not use_gdelt
    for q in queries:
        now = time.time()
        old = cache.get(q, {})
        r = {"query": q, "label": sources.label(q), "z_cut": z_cut, "spike": False, "stale": False, "error": "",
             "as_of": datetime.now().strftime("%I:%M %p"), "signal": "", "status": {}}
        results.append(r)

        items, r["status"] = sources.collect_all(q, pool=pool)
        r["_all"] = items  # the full list, for the crowd check; removed before saving
        r["items"] = [{k: it.get(k, "") for k in ("title", "url", "source", "platform", "kind", "image", "ts",
                                                   "author", "acct_created", "link") if it.get(k, "") != ""}
                      for it in items[:60]]
        z_m = latest_m = mean_m = 0.0
        bins, ok_m = [], False
        hist = [h for h in old.get("hist", []) if now - h[0] < 48 * 3600] if old.get("mix_v") == MIX_V else []
        # A source that answered with zero results is a real reading ("no mentions"), not a failure
        answered = any(isinstance(v, int) for v in r["status"].values())
        if items or answered:
            bins, latest_m = last_hour_mix(items, now)
            mean_m, z_m, ok_m = history_signal(hist, latest_m, now)
            hist.append([now, latest_m])
            r.update(summarize(items, now))
        r["hist"] = hist
        r["hist_hours"] = round((now - hist[0][0]) / 3600, 1) if hist else 0

        hours_g, z_g, latest_g, mean_g, ok_g = [], 0.0, 0.0, 0.0, False
        if not gdelt_busy and not sources.is_feed_line(q):  # a feed has no search words for GDELT
            try:
                hours_g = hourly(volume_series(q))
                _, z_g, latest_g, mean_g = spike_check(hours_g, z_cut, min_articles)
                ok_g = len(hours_g) >= 8
                r["status"]["GDELT"] = "ok"
            except GdeltBusy:
                gdelt_busy = True
                r["status"]["GDELT"] = "busy"
                print("    GDELT is busy; using the other sources for the rest of this round.")
            except Exception:
                r["status"]["GDELT"] = "error"
            time.sleep(PAUSE)
        else:
            r["status"]["GDELT"] = "n/a" if sources.is_feed_line(q) else "busy" if use_gdelt else "off"

        # Use whichever signal is more alarmed: GDELT counts worldwide news; the mix catches social-first stories.
        if ok_g and (z_g >= z_m or not ok_m):
            r.update(hours=hours_g, latest=latest_g, mean=mean_g, z=z_g, signal="worldwide news count (GDELT)")
            floor = min_articles
        elif ok_m or items or answered:
            r.update(hours=bins, latest=latest_m, mean=mean_m, z=z_m if ok_m else 0.0,
                     signal="all sources vs its own history" if ok_m
                     else f"all sources (learning normal: {r['hist_hours']}h of 2.5h)")
            floor = max(3, min_articles // 2)
        else:
            r.update({k: old.get(k) for k in ("hours", "latest", "mean", "z", "counts", "first", "tone",
                                              "headlines", "as_of", "signal")})
            cache[q] = dict(old, hist=hist, mix_v=MIX_V)
            r["stale"] = True
            for k, v in (("hours", []), ("latest", 0.0), ("mean", 0.0), ("z", 0.0)):
                r[k] = r[k] if r.get(k) is not None else v
            print(f"  {q}: no source answered; showing the saved reading")
            continue
        r["spike"] = r["z"] >= z_cut and r["latest"] >= floor
        cache[q] = {k: r.get(k) for k in ("hours", "latest", "mean", "z", "counts", "first", "tone",
                                           "headlines", "as_of", "signal", "hist")}
        cache[q]["mix_v"] = MIX_V
        live = ", ".join(f"{k} {v}" for k, v in r["status"].items())
        marker = "SPIKE" if r["spike"] else "ok"
        print(f"  {q}: last hour {r['latest']:.0f}, normal {r['mean']:.1f}, z={r['z']:.1f}  {marker}   [{live}]")
        time.sleep(2)

    # Crowd check for every topic, against what's normal across everything seen this round
    everything = [it for r in results for it in r.get("_all", [])]
    base_new = cib.baseline(everything)
    now = time.time()
    for r in results:
        if r.get("_all"):
            r["crowd"] = cib.check(r["_all"], now, base_new)
            try:
                r["graph"] = cib.graph(r["_all"], now)  # the Network view's data
            except Exception:
                r["graph"] = {"nodes": [], "edges": []}
        elif cache.get(r["query"], {}).get("crowd"):
            r["crowd"] = cache[r["query"]]["crowd"]
        else:
            r["crowd"] = cib.check([], now, base_new)
        r.pop("_all", None)
        cache.setdefault(r["query"], {})["crowd"] = r["crowd"]
        if r["crowd"]["level"] == "staged":
            print(f"  CROWD CHECK: '{r['label']}' looks staged ({r['crowd']['score']} points). Open it on the Radar.")
    save_cache(cache)
    if gdelt_busy and use_gdelt:
        print("GDELT was busy this round. The other sources still worked.")
    return results


def history_rows(results: list[dict], now: float, logged: dict) -> list[dict]:
    """One small line per topic per round for the weekly report. Once an hour (or whenever a spike or
    crowd warning happens) the line is a full one with counts, mood, headlines and evidence; the rest
    are short, to keep the history folder small."""
    rows = []
    hour = int(now // 3600)
    for r in results:
        crowd = r.get("crowd") or {}
        full = bool(r.get("spike") or crowd.get("flags") and logged.get(("f", r["query"])) != hour) or logged.get(r["query"]) != hour
        row = {"t": round(now), "q": r["query"], "label": r.get("label", r["query"]), "latest": r.get("latest", 0),
               "mean": round(r.get("mean") or 0, 2), "z": round(r.get("z") or 0, 2), "spike": bool(r.get("spike")),
               "crowd": {"level": crowd.get("level"), "score": crowd.get("score", 0), "social": crowd.get("social_posts", 0),
                         "flags": [{"kind": f["kind"], "title": f["title"]} for f in crowd.get("flags", [])]}}
        if full:
            row.update(tone=r.get("tone"), counts=r.get("counts") or {}, hist_hours=r.get("hist_hours", 0), stale=bool(r.get("stale")),
                       heads=[{k: h.get(k) for k in ("title", "url", "domain", "platform", "ts")} for h in (r.get("headlines") or [])[:3]])
            row["crowd"]["flags"] = [{"kind": f["kind"], "title": f["title"], "innocent": f.get("innocent", ""),
                                      "evidence": [{k: e.get(k) for k in ("title", "url", "who", "ts", "age_days") if e.get(k) is not None}
                                                   for e in f.get("evidence", [])[:3]]} for f in crowd.get("flags", [])]
            logged[r["query"]] = hour
            if crowd.get("flags"):
                logged[("f", r["query"])] = hour
        rows.append(row)
    return rows


def file_results(tenant: dict, results: list[dict], cooldown: dict, cooldown_min: int) -> None:
    """Alert and log spikes for one tenant (you or one client), using only that tenant's topics."""
    who = f" [{tenant['name']}]"
    for r in results:
        q = r["query"]
        ck = (tenant["slug"], q)
        if not r.get("spike") or time.time() - cooldown.get(ck, 0) < cooldown_min * 60:
            continue
        cooldown[ck] = time.time()
        heads = r.get("headlines", [])
        crowd = r.get("crowd") or {}
        print("\n" + "=" * 70)
        print(f"  SPIKE{who}: '{q}' is {r['latest']:.0f} this hour vs about {r['mean']:.0f} normally (z={r['z']:.1f}, {r['signal']})")
        print(f"  Crowd check: {crowd.get('label', 'n/a')} - {crowd.get('summary', '')}")
        for h in heads:
            print(f"   - {h.get('title', '').strip()}  [{h.get('platform', '')}: {h.get('domain', '')}]")
            print(f"     {h.get('url', '')}")
        print("  Next: confirm with 2 sources, then run your First Look prompt.")
        print("=" * 70 + "\n")
        top = heads[0] if heads else {}
        notify(f"Spike{who}: {r.get('label', q)}", top.get("title", f"{r['latest']:.0f} this hour"))
        log_alert({"time": datetime.now().isoformat(timespec="minutes"), "query": q, "z": round(r["z"], 1),
                   "last_hour": int(r["latest"]), "normal_hour": round(r["mean"], 1),
                   "top_headline": top.get("title", ""), "url": top.get("url", "")}, tenant["alerts"])


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))  # so the server finds its files wherever you start it
    p = argparse.ArgumentParser(description="Firm Radar: private monitoring for each client.")
    p.add_argument("--every", type=int, default=15, help="Minutes between checks (default 15)")
    p.add_argument("--z", type=float, default=3.0, help="How unusual a spike must be (default 3)")
    p.add_argument("--min", type=int, default=10, help="Minimum articles in the last hour to count (default 10)")
    p.add_argument("--cooldown", type=int, default=120, help="Minutes before the same topic can alert again")
    p.add_argument("--once", action="store_true", help="Check once and stop")
    p.add_argument("--no-open", action="store_true", help="Don't open the Firm Radar in the browser")
    p.add_argument("--no-gdelt", action="store_true", help="Skip GDELT and use only the other sources")
    p.add_argument("--port", type=int, default=8865, help="Port for the Firm Radar page (default 8865)")
    a = p.parse_args()

    url = server.start(a.port) if not a.once else ""
    print("FIRM RADAR - client monitoring")
    print(f"Mood engine: {mood.engine()}.  Open the Firm Radar at: {url or '(server off in --once mode)'}")
    print("Leave this window open. Close it to stop.")
    cooldown: dict = {}
    logged: dict = {}
    cache = load_cache()
    if url and not a.no_open:
        webbrowser.open(url)
    while True:
        started = time.time()
        tlist = tenants.all_tenants()  # re-read every round, so new clients and topics count right away
        wanted = {t["slug"]: load_watchlist(t["watchlist"]) for t in tlist}
        queries = list(dict.fromkeys(q for qs in wanted.values() for q in qs))  # each topic once
        health, results = {}, []
        if not tlist:
            print(f"\n[{datetime.now():%I:%M %p}] No clients yet. Click + New Client on the Firm Radar page to add one.")
        else:
            print(f"\n[{datetime.now():%I:%M %p}] Watching for: {', '.join(t['name'] for t in tlist)}  ({len(queries)} topics)")
            print("Reading outlets and video channels...")
            arts, vids, health = feeds.collect()
            results = check_all(queries, a.z, a.min, cooldown, a.cooldown, use_gdelt=not a.no_gdelt,
                                cache=cache, pool=arts + vids)
            for r in results:
                for k, v in (r.get("status") or {}).items():
                    if k not in ("Outlets", "YouTube"):
                        prev = health.get(k)
                        health[k] = v if not isinstance(prev, int) else prev + (v if isinstance(v, int) else 0)
        now = time.time()
        by_q = {r["query"]: r for r in results}
        for t in tlist:
            mine = [by_q[q] for q in wanted[t["slug"]] if q in by_q]
            file_results(t, mine, cooldown, a.cooldown)
            try:
                tenants.append_history(t, history_rows(mine, now, logged.setdefault(t["slug"], {})), now)
            except OSError as exc:
                print(f"  Couldn't save history for {t['name']}: {exc}")
            server.write_data({"generated": now, "client": t["slug"], "client_name": t["name"], "topics": mine,
                               "alerts": tenants.read_alerts(t["alerts"], limit=50)}, t["data"])
        server.write_data({"generated": now, "generated_text": datetime.now().strftime("%I:%M %p"), "every_min": a.every,
                           "next_at": now + a.every * 60, "round_secs": round(now - started), "health": health,
                           "mood_engine": mood.engine(), "clients": len(tlist)})
        print(f"Round done in {round(now - started)}s. Firm Radar: {url or '(server off)'}")
        if a.once:
            break
        server.check_now.clear()
        server.check_now.wait(timeout=max(1, a.every) * 60)  # sleep until the next round, or until "Check now"


if __name__ == "__main__":
    main()
