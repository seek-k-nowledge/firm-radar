"""
report.py - Turns a week of Radar data into a clean written brief for a client.

Plain English: every round, the Radar saves one small line per topic (history/). This file reads
the last 7 days of those lines for one client and writes a brief that answers what a client pays
for: what moved, what looked coordinated, and what to watch next. Every finding carries a
confidence level, the way professional analysts write.

The brief opens in your browser. Click "Edit text" to fix anything, "Save edits", then
"Print / Save as PDF" to send it. It is a DRAFT until you've read it: the program counts and
compares; you make the judgment calls.

Standard terms: automated intelligence reporting. "BLUF" = bottom line up front.
Confidence levels: High / Moderate / Low = how solid the evidence is. Estimative words
(unlikely, roughly even chance, likely...) = how probable we think something is.
These follow the US intelligence community's writing standard (ICD 203).

Run by hand:  python report.py acme-water        (a client, last 7 days)
              python report.py acme-water 14     (last 14 days)
"""

import csv
import html
import json
import os
import statistics
import sys
import time
from datetime import datetime

import tenants

E = html.escape
AUTHOR = "Paul Edward Harris IV"
LEVEL_ORDER = {"staged": 3, "mixed": 2, "organic": 1, "thin": 0, None: -1}


def _when(ts: float, fmt: str = "%a %b %d, %I:%M %p") -> str:
    return datetime.fromtimestamp(ts).strftime(fmt).replace(" 0", " ") if ts else ""


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _alerts(path: str, since: float) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["time"]).timestamp()
        except (KeyError, ValueError):
            continue
        if t >= since:
            out.append(dict(r, t=t))
    return out


def _rows_from_snapshot(data: dict) -> list[dict]:
    """No history yet (the Radar was just updated)? Use the latest round as a one-round history."""
    rows = []
    for r in data.get("topics") or []:
        crowd = r.get("crowd") or {}
        rows.append({"t": data.get("generated", time.time()), "q": r["query"], "label": r.get("label", r["query"]),
                     "latest": r.get("latest", 0), "mean": r.get("mean", 0), "z": r.get("z", 0), "spike": r.get("spike"),
                     "tone": r.get("tone"), "counts": r.get("counts") or {}, "hist_hours": r.get("hist_hours", 0),
                     "crowd": {"level": crowd.get("level"), "score": crowd.get("score", 0),
                               "social": crowd.get("social_posts", 0), "flags": crowd.get("flags", [])},
                     "heads": [{k: h.get(k) for k in ("title", "url", "domain", "platform", "ts")}
                               for h in (r.get("headlines") or [])[:4]]})
    return rows


def _per_day(row: dict) -> float:
    return float(sum(v for v in (row.get("counts") or {}).values() if isinstance(v, (int, float))))


def summarize(rows: list[dict], prior: list[dict]) -> list[dict]:
    by_q, prior_q = {}, {}
    for r in rows:
        by_q.setdefault(r["q"], []).append(r)
    for r in prior:
        prior_q.setdefault(r["q"], []).append(r)
    topics = []
    for q, rs in by_q.items():
        rs.sort(key=lambda r: r["t"])
        first_t, last_t = rs[0]["t"], rs[-1]["t"]
        vols = [_per_day(r) for r in rs if "counts" in r]
        avg = statistics.mean(vols) if vols else 0
        pv = [_per_day(r) for r in prior_q.get(q, []) if "counts" in r]
        prior_avg = statistics.mean(pv) if pv else None
        change = (avg - prior_avg) / prior_avg if prior_avg else None
        peak = max(rs, key=lambda r: r.get("z") or 0)
        # spike events: spike rounds closer than 2 hours apart are one event
        events, last = [], None
        for r in rs:
            if r.get("spike"):
                if last is None or r["t"] - last > 7200:
                    events.append(r)
                last = r["t"]
        tones_a = [r["tone"] for r in rs if r.get("tone") is not None and r["t"] - first_t <= 86400]
        tones_b = [r["tone"] for r in rs if r.get("tone") is not None and last_t - r["t"] <= 86400]
        plats = sorted({k for r in rs for k, v in (r.get("counts") or {}).items() if v})
        levels = [((r.get("crowd") or {}).get("level")) for r in rs]
        worst = max(levels, key=lambda l: LEVEL_ORDER.get(l, -1)) if levels else None
        flag_kinds = {}
        for r in rs:
            for f in (r.get("crowd") or {}).get("flags", []):
                k = flag_kinds.setdefault(f["kind"], {"rounds": 0, "title": f["title"], "innocent": f.get("innocent", ""),
                                                      "evidence": f.get("evidence", []), "t": r["t"]})
                k["rounds"] += 1
                if r["t"] >= k["t"]:
                    k.update(title=f["title"], t=r["t"])
                if f.get("evidence"):
                    k["evidence"] = f["evidence"]
                if f.get("innocent"):
                    k["innocent"] = f["innocent"]
        social = max(((r.get("crowd") or {}).get("social", 0) for r in rs), default=0)
        heads_at_peak = next((r["heads"] for r in sorted(rs, key=lambda r: abs(r["t"] - peak["t"])) if r.get("heads")), [])
        latest_heads = next((r["heads"] for r in reversed(rs) if r.get("heads")), [])
        cover_h = (last_t - first_t) / 3600 + max(0.0, min(48.0, rs[0].get("hist_hours") or 0))
        if cover_h >= 120 and len(plats) >= 2 and len(rs) >= 100:
            conf = "High"
        elif cover_h >= 24 and plats:
            conf = "Moderate"
        else:
            conf = "Low"
        topics.append({"q": q, "label": rs[0].get("label", q), "rounds": len(rs), "first": first_t, "last": last_t,
                       "avg": avg, "prior_avg": prior_avg, "change": change, "peak_z": peak.get("z") or 0,
                       "peak_t": peak["t"], "peak_heads": heads_at_peak, "latest_heads": latest_heads,
                       "events": events, "tone_a": statistics.mean(tones_a) if tones_a else None,
                       "tone_b": statistics.mean(tones_b) if tones_b else None, "platforms": plats,
                       "z_now": rs[-1].get("z") or 0, "worst": worst, "flags": flag_kinds, "social": social,
                       "levels": levels, "conf": conf})
    return topics


def _conf(level: str, why: str = "") -> str:
    plain = {"High": "we're quite sure", "Moderate": "fairly sure, some gaps", "Low": "early or thin evidence"}.get(level, "")
    return f'<span class="conf c-{level.lower()}" title="{E(why)}">{level} confidence</span> <span class="why">{E(plain)}</span>'


def _mood(t) -> str:
    if t is None:
        return "no read"
    word = "positive" if t >= .05 else "neutral" if t > -.05 else "tense" if t > -.25 else "hostile / fearful"
    return f"{word} ({t:+.2f})"


def _heads(heads: list, n: int = 3) -> str:
    if not heads:
        return ""
    return "<ul class='heads'>" + "".join(
        f"<li><a href='{E(h.get('url') or '')}'>{E(h.get('title') or '')}</a> <span class='src'>{E(h.get('domain') or h.get('platform') or '')}</span></li>"
        for h in heads[:n]) + "</ul>"


def _change(t) -> str:
    if t["change"] is None:
        return "no earlier week to compare"
    return f"{'up' if t['change'] >= 0 else 'down'} {abs(t['change']):.0%} from the period before"


def build(slug: str, days: int = 7, now: float | None = None) -> dict:
    now = now or time.time()
    p = tenants.paths(slug)
    info = _read_json(p["info"], {})
    since = now - days * 86400
    rows = tenants.load_history(p, since, now + 60)
    prior = tenants.load_history(p, since - days * 86400, since)
    snapshot = False
    if not rows:
        data = _read_json(p["data"], {})
        rows, snapshot = _rows_from_snapshot(data), True
    topics = summarize(rows, prior)
    alerts = _alerts(p["alerts"], since)
    pins = [x for x in _read_json(p["pins"], []) if x.get("note")]
    client = p["name"]

    cover_from = min((t["first"] for t in topics), default=now)
    cover_h = (now - cover_from) / 3600
    total_rounds = len({r["t"] for r in rows})

    # ---------- findings ----------
    movers = sorted(topics, key=lambda t: (t["change"] if t["change"] is not None else 0, t["avg"]), reverse=True)
    by_vol = sorted(topics, key=lambda t: -t["avg"])
    spiked = sorted([t for t in topics if t["events"]], key=lambda t: -t["peak_z"])
    staged = [t for t in topics if t["worst"] == "staged"]
    mixed = [t for t in topics if t["worst"] == "mixed"]
    thin = [t for t in topics if t["worst"] in ("thin", None)]
    soured = sorted([t for t in topics if t["tone_a"] is not None and t["tone_b"] is not None and t["tone_b"] - t["tone_a"] <= -0.1],
                    key=lambda t: t["tone_b"] - t["tone_a"])

    bluf = []
    if by_vol:
        top = by_vol[0]
        bluf.append(f"<b>{E(top['label'])}</b> drew the most coverage: about {top['avg']:.0f} stories and posts a day "
                    f"({_change(top)}).")
    risers = [t for t in movers if t["change"] is not None and t["change"] >= 0.5]
    if risers:
        bluf.append("Rising fastest: " + ", ".join(f"<b>{E(t['label'])}</b> ({t['change']:+.0%})" for t in risers[:3]) + ".")
    if spiked:
        s0 = spiked[0]
        n_ev = sum(len(t["events"]) for t in spiked)
        bluf.append(f"{n_ev} unusual surge{'s' if n_ev != 1 else ''} across {len(spiked)} topic{'s' if len(spiked) != 1 else ''}. "
                    f"The biggest was <b>{E(s0['label'])}</b> on {_when(s0['peak_t'])} "
                    f"({s0['peak_z']:.1f}× its normal swings).")
    else:
        bluf.append("No unusual surges on any watched topic this period.")
    if staged or mixed:
        parts = []
        if staged:
            parts.append(f"looked staged at least once: {', '.join('<b>' + E(t['label']) + '</b>' for t in staged)}")
        if mixed:
            parts.append(f"showed one warning sign: {', '.join(E(t['label']) for t in mixed[:5])}")
        bluf.append("Crowd check: " + "; ".join(parts) + ". Details and innocent explanations below.")
    else:
        bluf.append("Crowd check: no sign of a staged crowd on any topic"
                    + (f" (though {len(thin)} topic{'s' if len(thin) != 1 else ''} had too few social posts to check)." if thin else "."))
    if soured:
        t = soured[0]
        bluf.append(f"Mood turned more negative on <b>{E(t['label'])}</b> ({_mood(t['tone_a'])} → {_mood(t['tone_b'])}).")

    # ---------- sections ----------
    cov_note = ""
    if snapshot:
        cov_note = ("<div class='callout'><b>Early read.</b> This brief is built from the Radar's latest round only, because the "
                    "Radar has only just started saving history. From now on it saves every round, so next week's brief covers the full week.</div>")
    elif total_rounds < 8:
        cov_note = (f"<div class='callout'><b>Early read.</b> The Radar started saving history for this Radar on {_when(cover_from)}, "
                    f"so this brief rests on {total_rounds} reading{'s' if total_rounds != 1 else ''}. Next week's brief covers the full week.</div>")
    elif cover_h < days * 24 - 6:
        cov_note = (f"<div class='callout'><b>Partial week.</b> The Radar has {cover_h:.0f} hours of saved history for this period "
                    f"(out of {days * 24}). Treat week-over-week comparisons as preliminary.</div>")

    moved_rows = "".join(
        f"<tr><td><b>{E(t['label'])}</b></td><td>{t['avg']:.0f}</td><td>{E(_change(t))}</td>"
        f"<td>{len(t['events']) or '—'}</td><td>{t['peak_z']:.1f}</td><td>{E(_mood(t['tone_b']))}</td>"
        f"<td>{E(', '.join(t['platforms']) or '—')}</td><td>{t['conf']}</td></tr>"
        for t in by_vol)
    moved_detail = ""
    for t in (spiked + [x for x in by_vol if x not in spiked])[:5]:
        what = (f"Surged {len(t['events'])} time{'s' if len(t['events']) != 1 else ''}; peak on {_when(t['peak_t'])} at "
                f"{t['peak_z']:.1f}× normal swings." if t["events"] else
                f"No surge. Steady at about {t['avg']:.0f} a day ({_change(t)}).")
        why = (f"{t['rounds']} Radar readings over {(t['last'] - t['first']) / 3600:.0f} hours, from "
               f"{len(t['platforms'])} source type{'s' if len(t['platforms']) != 1 else ''}.")
        moved_detail += (f"<div class='finding'><h4>{E(t['label'])}</h4><p>{what} Mood: {E(_mood(t['tone_a']))} → {E(_mood(t['tone_b']))}.</p>"
                         f"<p>{_conf(t['conf'], why)}</p>{_heads(t['peak_heads'] if t['events'] else t['latest_heads'])}"
                         f"<p class='analyst' data-ph='1'>Analyst read: [why this matters to {E(client)}, or delete]</p></div>")

    coord = ""
    for t in sorted(staged + mixed, key=lambda t: -LEVEL_ORDER.get(t["worst"], 0)):
        n_flag_rounds = sum(1 for l in t["levels"] if l in ("staged", "mixed"))
        if t["worst"] == "staged":
            judgment = ("We assess it is likely that part of this conversation is coordinated rather than organic."
                        if t["social"] >= 15 else
                        "We assess there is a roughly even chance that part of this conversation is coordinated rather than organic.")
            conf = "Moderate" if t["social"] >= 15 and n_flag_rounds >= 3 else "Low"
        else:
            judgment = "This is probably organic, but one warning sign needs checking before ruling it out."
            conf = "Low"
        signs = "".join(
            f"<li><b>{E(f['title'])}</b> <span class='src'>(seen in {f['rounds']} reading{'s' if f['rounds'] != 1 else ''})</span>"
            f"<div class='innocent'>Innocent explanation to rule out: {E(f['innocent'])}</div>"
            + ("<ul class='ev'>" + "".join(
                f"<li><a href='{E(e.get('url') or '')}'>{E((e.get('title') or '')[:140])}</a> "
                f"<span class='src'>{E(e.get('who') or '')}{(' · account ' + str(e['age_days']) + ' days old') if e.get('age_days') is not None else ''}"
                f"{(' · ' + _when(e['ts'])) if e.get('ts') else ''}</span></li>" for e in f.get("evidence", [])[:4]) + "</ul>")
            + "</li>" for f in t["flags"].values())
        coord += (f"<div class='finding flag-{t['worst']}'><h4>{E(t['label'])} <span class='lvl'>{'LOOKS STAGED' if t['worst'] == 'staged' else 'WORTH A LOOK'}</span></h4>"
                  f"<p>{judgment}</p><p>{_conf(conf, 'Automated pattern check on public posts. Hand-checking the accounts can raise this.')}</p>"
                  f"<ul class='signs'>{signs}</ul><p class='analyst' data-ph='1'>Analyst check: [what you found when you looked at the accounts by hand]</p></div>")
    if not coord:
        coord = ("<p>No topic showed a staged-crowd pattern this period.</p>"
                 + (f"<p class='src'>Too few social posts to check: {E(', '.join(t['label'] for t in thin))}. These were mostly covered by "
                    "news outlets, where the crowd check has little to look at.</p>" if thin else ""))

    watch = []
    for t in sorted(topics, key=lambda t: -t["z_now"]):
        if 1.5 <= t["z_now"] < 3:
                watch.append(f"<li><b>{E(t['label'])}</b> is climbing right now ({t['z_now']:.1f}× normal swings) but hasn't surged. "
                             f"<i>Action:</i> keep an eye on it over the next 24 hours.</li>")
    for t in risers:
        if not any(t["label"] in w for w in watch):
            watch.append(f"<li><b>{E(t['label'])}</b> is up {t['change']:.0%} on the period before. <i>Action:</i> check whether a new "
                         f"angle or new voices are driving it.</li>")
    recent_flag = [t for t in staged + mixed if any(r for r in t["flags"].values() if now - r["t"] <= 172800)]
    for t in recent_flag:
        watch.append(f"<li><b>{E(t['label'])}</b> had a crowd-check warning in the last 48 hours. <i>Action:</i> check the accounts by hand "
                     f"before saying anything publicly.</li>")
    comps = set(c.lower() for c in info.get("competitors", []))
    for t in topics:
        if t["label"].lower() in comps and (t["events"] or (t["change"] or 0) >= 0.3):
            watch.append(f"<li>Competitor <b>{E(t['label'])}</b> got more attention this period. <i>Action:</i> see what they're "
                         f"saying, and whether it touches {E(client)}'s issues.</li>")
    if not watch:
        watch.append("<li>Nothing is building right now. The Radar keeps watching and will alert on any surge.</li>")

    pin_html = "".join(f"<li><a href='{E(x.get('url', ''))}'>{E(x.get('title', ''))}</a> <span class='src'>[{E(x.get('tag', ''))}]</span>"
                       f"<div>{E(x.get('note', ''))}</div></li>" for x in pins[:12])
    alert_rows = "".join(f"<tr><td>{E(_when(a['t']))}</td><td>{E(a.get('query', ''))}</td><td>{E(a.get('z', ''))}</td>"
                         f"<td><a href='{E(a.get('url', ''))}'>{E(a.get('top_headline', ''))}</a></td></tr>" for a in alerts[-25:][::-1])
    issues = ", ".join(info.get("issues", [])) or "—"
    comps_txt = ", ".join(info.get("competitors", [])) or "—"

    period = f"{_when(since, '%b %d')} – {_when(now, '%b %d, %Y')}"
    body = f"""
<div class="banner">CONFIDENTIAL · PREPARED FOR {E(client.upper())}</div>
<header class="cover">
  <div class="kind">WEEKLY NARRATIVE BRIEF</div>
  <h1>{E(client)}</h1>
  <div class="meta">Period: {E(period)} · Prepared by {E(AUTHOR)} · Generated {E(_when(now))}</div>
  <div class="draft" data-ph="1">DRAFT: read and edit before sending. Delete this line when it's ready.</div>
</header>

<section><h2>Bottom line</h2><ul class="bluf">{''.join(f'<li>{b}</li>' for b in bluf)}</ul>{cov_note}</section>

<section><h2>What moved</h2>
<table><thead><tr><th>Topic</th><th>Per day</th><th>Change</th><th>Surges</th><th>Peak</th><th>Mood now</th><th>Seen on</th><th>Confidence</th></tr></thead>
<tbody>{moved_rows or '<tr><td colspan=8>No data yet.</td></tr>'}</tbody></table>
<p class="src">"Per day" = average number of stories and posts found in any 24 hours. "Peak" = how far the busiest hour sat above normal swings (3 or more counts as a surge).</p>
{moved_detail}</section>

<section><h2>What looked coordinated</h2>
<p class="src">The crowd check looks for four warning signs: copy-paste wording across many accounts, a burst of brand-new accounts, one link pushed hard in a short time, and a few accounts doing most of the talking. It finds patterns, not proof.</p>
{coord}</section>

<section><h2>What to watch next</h2><ul class="watch">{''.join(watch)}</ul></section>

<section><h2>Analyst notes</h2>
{f'<ul class="pins">{pin_html}</ul>' if pin_html else '<p class="analyst" data-ph="1">[Your own read of the week: context the numbers miss, who benefits, what you recommend.]</p>'}
</section>

{f'<section><h2>Alert log</h2><table><thead><tr><th>Time</th><th>Topic</th><th>z</th><th>Top headline</th></tr></thead><tbody>{alert_rows}</tbody></table></section>' if alert_rows else ''}

<section class="method"><h2>How to read this brief</h2>
<p><b>Confidence</b> says how solid the evidence is. <b>High</b>: several days of data from more than one kind of source. <b>Moderate</b>: at least a day of data, with some gaps. <b>Low</b>: early, thin, or from one kind of source only. Automated coordination findings are never rated High; checking the accounts by hand is what raises them.</p>
<p><b>Likelihood words</b> follow the standard scale: almost no chance · very unlikely · unlikely · roughly even chance · likely · very likely · almost certain.</p>
<p><b>Sources:</b> public news search (Google News, Bing News, GDELT), public social posts (Bluesky, Mastodon, Reddit, Hacker News), and a set of outlet and video feeds, checked every 15 minutes by the Firm Radar. Watched for {E(client)}: {E(issues)}; competitors/opponents: {E(comps_txt)}. Coverage this period: {cover_h:.0f} hours, {total_rounds} readings.</p>
<p><b>Limits:</b> the Radar sees public posts only. It can't see private groups, closed platforms, paid ads, or who runs an account. Surges are measured against each topic's own recent normal, so a topic that's always loud won't register as a surge.</p>
</section>
<div class="banner">CONFIDENTIAL · PREPARED FOR {E(client.upper())}</div>
"""
    os.makedirs(p["reports"], exist_ok=True)
    stem = datetime.fromtimestamp(now).strftime("%Y-%m-%d") + "-weekly-brief"
    name, n = stem + ".html", 2
    while os.path.exists(os.path.join(p["reports"], name)):
        name, n = f"{stem}-{n}.html", n + 1
    page = TEMPLATE.replace("%%TITLE%%", E(f"{client} · Weekly Narrative Brief · {period}")) \
        .replace("%%CLIENT%%", E(slug)).replace("%%FILE%%", E(name)).replace("%%BODY%%", body)
    with open(os.path.join(p["reports"], name), "w", encoding="utf-8") as f:
        f.write(page)
    return {"file": name, "url": f"/report?client={slug}&file={name}", "topics": len(topics),
            "hours": round(cover_h), "snapshot": snapshot, "folder": os.path.abspath(p["reports"])}


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="radar-client" content="%%CLIENT%%"><meta name="radar-file" content="%%FILE%%">
<title>%%TITLE%%</title>
<style>
:root{--ink:#14202e;--dim:#5a6878;--line:#d5dce4;--navy:#0f2740;--red:#b3261e;--amber:#9a6200;--green:#1e6b3c;--bg:#fff}
*{box-sizing:border-box}
body{margin:0;background:#eef1f4;color:var(--ink);font:15px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif}
#doc{max-width:860px;margin:64px auto 40px;background:var(--bg);padding:28px 44px 30px;box-shadow:0 1px 6px rgba(0,0,0,.12)}
.banner{text-align:center;font:700 11px/1 "Segoe UI",sans-serif;letter-spacing:.18em;color:var(--red);padding:8px 0}
.cover{border-bottom:3px solid var(--navy);padding-bottom:14px;margin-bottom:18px}
.kind{font-size:12px;letter-spacing:.2em;color:var(--dim);font-weight:700}
h1{font:700 30px/1.15 Georgia,"Times New Roman",serif;margin:6px 0;color:var(--navy)}
h2{font:700 13px/1 "Segoe UI",sans-serif;letter-spacing:.16em;text-transform:uppercase;color:var(--navy);border-bottom:1px solid var(--line);padding-bottom:6px;margin:26px 0 10px}
h4{margin:0 0 4px;font-size:15.5px}
.meta,.src,.why{color:var(--dim);font-size:12.5px}
.draft{margin-top:8px;display:inline-block;background:#fff4d6;border:1px solid #e8c766;padding:3px 8px;font-size:12px}
.bluf li{margin:5px 0}
table{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0}
th,td{text-align:left;padding:6px 7px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;letter-spacing:.06em;color:var(--dim);text-transform:uppercase}
.finding{border-left:3px solid var(--navy);padding:6px 0 6px 14px;margin:14px 0}
.finding.flag-staged{border-left-color:var(--red)} .finding.flag-mixed{border-left-color:var(--amber)}
.lvl{font-size:10.5px;letter-spacing:.12em;padding:1px 6px;border:1px solid currentColor;margin-left:6px;vertical-align:2px}
.flag-staged .lvl{color:var(--red)} .flag-mixed .lvl{color:var(--amber)}
.conf{font-size:11.5px;font-weight:700;padding:1px 7px;border-radius:2px}
.c-high{background:#dcefe3;color:var(--green)} .c-moderate{background:#fff0cf;color:var(--amber)} .c-low{background:#f6dcda;color:var(--red)}
.heads,.ev{margin:4px 0 0;padding-left:18px;font-size:13.5px} .heads a,.ev a,.pins a{color:var(--navy)}
.innocent{font-size:13px;color:var(--dim);margin:2px 0 4px}
.signs{padding-left:18px}
.analyst{color:#7a5a00;background:#fffaf0;border:1px dashed #e8c766;padding:4px 8px;font-size:13.5px}
.callout{background:#f1f5f9;border:1px solid var(--line);padding:8px 12px;margin-top:10px;font-size:13.5px}
.method p{font-size:13px;color:#344150}
#bar{position:fixed;top:0;left:0;right:0;z-index:9;background:var(--navy);color:#fff;display:flex;gap:8px;align-items:center;padding:9px 14px;font-size:13px;flex-wrap:wrap}
#bar button{font:inherit;background:#1d3d5f;color:#fff;border:1px solid #3d6187;padding:5px 12px;cursor:pointer}
#bar button.on{background:#ffd166;color:#000;border-color:#ffd166}
#bar span{opacity:.85;margin-left:auto}
body.editing #doc{outline:3px dashed #ffd166;outline-offset:6px}
@media(max-width:700px){#doc{padding:18px 16px;margin-top:96px}}
@media print{#bar{display:none}body{background:#fff}#doc{box-shadow:none;margin:0;max-width:none;padding:0}.analyst[data-ph],.draft{display:none}a{color:inherit;text-decoration:none}}
</style></head>
<body>
<div id="bar"><b>Weekly brief</b><button id="edit">✎ Edit text</button><button id="save">Save edits</button><button id="print">Print / Save as PDF</button>
<span id="msg">Read it, fix anything, then print to PDF. Yellow boxes are for your own read; any left untouched won't print.</span></div>
<main id="doc">%%BODY%%</main>
<script>
const doc=document.getElementById("doc"),msg=document.getElementById("msg");
const client=document.querySelector('meta[name=radar-client]').content,file=document.querySelector('meta[name=radar-file]').content;
document.getElementById("edit").onclick=e=>{const on=doc.isContentEditable;doc.contentEditable=on?"false":"true";document.body.classList.toggle("editing",!on);e.target.classList.toggle("on",!on);e.target.textContent=on?"✎ Edit text":"Done editing";msg.textContent=on?"Remember to save.":"Click anywhere in the brief and type. Anything you change in a yellow box will print.";};
doc.addEventListener("input",()=>{let n=(getSelection()||{}).anchorNode;if(n&&n.nodeType===3)n=n.parentNode;const a=n&&n.closest&&n.closest("[data-ph]");if(a)a.removeAttribute("data-ph");});
document.getElementById("save").onclick=async()=>{doc.contentEditable="false";document.body.classList.remove("editing");const b=document.getElementById("edit");b.classList.remove("on");b.textContent="✎ Edit text";
 try{const r=await fetch("/api/report-save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({client,file,html:"<!doctype html>\\n"+document.documentElement.outerHTML})});const j=await r.json();msg.textContent=j.saved?"Saved.":"Couldn't save: "+(j.error||"");}catch(err){msg.textContent="Couldn't save. Is the Radar running?";}};
document.getElementById("print").onclick=()=>window.print();
</script>
</body></html>
"""


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if len(sys.argv) < 2:
        sys.exit("Usage: python report.py <client-folder-name> [days]")
    slug = sys.argv[1]
    d = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    out = build(slug, d)
    print("Brief written:", os.path.join(out["folder"], out["file"]))
