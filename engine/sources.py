"""
sources.py - Where the Narrative Radar gets its information.

Plain English: instead of asking one news database, the radar asks several free places at once.
Each one sees different things: big news sites, smaller outlets, tech people, social media.
A story that shows up on social media hours before the news is exactly what you want to catch.

Every function here returns a list of "items", each one a dict:
    {"title", "url", "source", "platform", "ts"}   (ts = time posted, in seconds since 1970, UTC)
Social posts also carry "author", "acct_created" (when the account was made) and "link" (an
outside link they share), which the fake-crowd detector (cib.py) uses.

Standard terms: these are "collectors" or "connectors" (one per data source), feeding an
"aggregator". RSS is a simple feed format most news sites publish; JSON APIs are machine-readable
answers from a website.

None of these need an account or a key. If one is down or blocks you, it is skipped and the
others keep working.
"""

import html
import json
import re
import shlex
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

FILTER_RE = re.compile(r"^(sourcelang|sourcecountry|domain|domainis|theme|tone|near\d*|repeat\d*):", re.I)  # GDELT filters, not words
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) narrative-radar/0.3 (personal research)"


def search_terms(watch_line: str) -> str:
    """Turn a watchlist line like  "deepfake" sourcelang:english  into plain search words: deepfake.
    (sourcelang:/sourcecountry: are GDELT-only filters; other sites don't understand them.)"""
    try:
        parts = shlex.split(watch_line)
    except ValueError:
        parts = watch_line.split()
    words = [p for p in parts if not FILTER_RE.match(p)]
    return " ".join(words).strip() or watch_line


def phrase_query(watch_line: str) -> str:
    """For Google/Bing: keep each quoted phrase as its own exact phrase, e.g.  "AI" "cyberattack"
    means both words somewhere in the story, not the exact phrase "AI cyberattack"."""
    try:
        parts = shlex.split(watch_line)
    except ValueError:
        parts = watch_line.split()
    return " ".join(f'"{p}"' for p in parts if not FILTER_RE.match(p)) or watch_line


def topic_parts(watch_line: str) -> list[str]:
    try:
        parts = shlex.split(watch_line)
    except ValueError:
        parts = watch_line.split()
    return [p for p in parts if not FILTER_RE.match(p)]


def is_feed_line(watch_line: str) -> bool:
    return watch_line.strip().lower().startswith("feed:")


def feed_parts(watch_line: str) -> tuple[str, str]:
    """A feed line looks like:  feed:https://example.com/rss title:My feed name   -> (url, title)"""
    body = watch_line.strip()[5:].strip()
    url, _, title = body.partition(" title:")
    return url.strip(), title.strip()


def feed_platform(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    if host.endswith("google.com") and "/alerts/" in url:
        return "Google Alerts"
    for name, h in (("YouTube", "youtube.com"), ("GitHub", "github.com"), ("Bluesky", "bsky.app"), ("Substack", "substack.com"),
                    ("Medium", "medium.com"), ("Mastodon", "mastodon.social"), ("Reddit", "reddit.com")):
        if host.endswith(h):
            return name
    return "Feeds"


def label(watch_line: str) -> str:
    """Short display name: "AI" "cyberattack" sourcelang:english  ->  AI + cyberattack"""
    if is_feed_line(watch_line):
        url, title = feed_parts(watch_line)
        return title or (feed_platform(url) + ": " + urllib.parse.urlparse(url).netloc.removeprefix("www."))
    return " + ".join(topic_parts(watch_line)) or watch_line


def matcher(watch_line: str):
    """Returns a function that says whether a headline is about this topic: every quoted part must
    appear in it. "AI" is matched as a whole word (so 'said' or 'Taiwan' don't count)."""
    pats = []
    for p in topic_parts(watch_line):
        if p.lower() in ("ai", "a.i."):
            pats.append(re.compile(r"\b(AI|A\.I\.)\b|artificial intelligence", re.I))
        else:
            pats.append(re.compile(r"\b" + re.escape(p) + r"", re.I))
    return lambda text: bool(pats) and all(pt.search(text or "") for pt in pats)


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _rss_time(text: str) -> float:
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _iso(text: str) -> float:
    """Turn a time like 2026-10-13T19:37:02.602Z into seconds since 1970 (0 if missing)."""
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def google_news(terms: str) -> list[dict]:
    """Google News search feed: thousands of outlets, big and small. Last 2 days, up to 100 stories."""
    q = urllib.parse.quote(f"{terms} when:2d")
    root = ET.fromstring(_get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"))
    out = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title", ""))
        src_el = it.find("source")
        source = src_el.text if src_el is not None and src_el.text else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        out.append({"title": title, "url": it.findtext("link", ""), "source": source,
                    "platform": "Google News", "kind": "article", "ts": _rss_time(it.findtext("pubDate", ""))})
    return out


def bing_news(terms: str) -> list[dict]:
    """Bing News search feed: a different index from Google, so it catches different outlets."""
    q = urllib.parse.quote(terms)
    root = ET.fromstring(_get(f"https://www.bing.com/news/search?q={q}&format=rss"))
    out = []
    for it in root.iter("item"):
        link = it.findtext("link", "")
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        source, image = "", ""
        for child in it:
            if child.tag.endswith("Source") and child.text:
                source = child.text
            if child.tag.endswith("Image") and child.text:
                image = child.text.replace("http://", "https://")
        out.append({"title": _clean(it.findtext("title", "")), "url": real, "source": source, "image": image,
                    "platform": "Bing News", "kind": "article", "ts": _rss_time(it.findtext("pubDate", ""))})
    return out


def hacker_news(terms: str) -> list[dict]:
    """Hacker News: where tech and AI people talk. Often early on AI and security stories."""
    q = urllib.parse.quote(terms)
    data = json.loads(_get(f"https://hn.algolia.com/api/v1/search_by_date?query={q}&tags=story&hitsPerPage=100"))
    out = []
    for h in data.get("hits", []):
        out.append({"title": _clean(h.get("title") or ""),
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                    "source": "news.ycombinator.com", "platform": "Hacker News", "kind": "post",
                    "ts": float(h.get("created_at_i") or 0), "author": h.get("author", ""), "link": h.get("url") or ""})
    return out


def mastodon(terms: str) -> list[dict]:
    """Mastodon: open social network. Searches the hashtag version of the topic, e.g. #deepfake."""
    tag = re.sub(r"[^A-Za-z0-9]", "", terms)
    if not tag:
        return []
    data = json.loads(_get(f"https://mastodon.social/api/v1/timelines/tag/{tag}?limit=40"))
    out = []
    for s in data:
        text = _clean(s.get("content", ""))
        if not text:
            continue
        try:
            ts = datetime.fromisoformat(s.get("created_at", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0.0
        account = s.get("account") or {}
        acct = account.get("acct", "")
        media = [m.get("preview_url") for m in s.get("media_attachments") or [] if m.get("type") == "image" and m.get("preview_url")]
        card = (s.get("card") or {}).get("image") or ""
        out.append({"title": text[:160] + ("..." if len(text) > 160 else ""), "text": text, "url": s.get("url", ""),
                    "source": "@" + acct if acct else "mastodon", "platform": "Mastodon", "kind": "post",
                    "image": (media[0] if media else card) if not s.get("sensitive") else "", "ts": ts,
                    "author": acct, "acct_created": _iso(account.get("created_at", "")),
                    "followers": account.get("followers_count"), "link": (s.get("card") or {}).get("url", ""),
                    "link_title": _clean((s.get("card") or {}).get("title", ""))})
    return out


def reddit(terms: str) -> list[dict]:
    """Reddit: public discussion. Often blocks automated requests; skipped automatically if it does."""
    q = urllib.parse.quote(f'"{terms}"')
    data = json.loads(_get(f"https://www.reddit.com/search.json?q={q}&sort=new&limit=100&t=week"))
    out = []
    for c in data.get("data", {}).get("children", []):
        d = c.get("data", {})
        out.append({"title": _clean(d.get("title", "")), "url": "https://www.reddit.com" + d.get("permalink", ""),
                    "source": "r/" + d.get("subreddit", ""), "platform": "Reddit", "kind": "post",
                    "ts": float(d.get("created_utc") or 0), "author": d.get("author", ""),
                    "link": "" if d.get("is_self") else d.get("url", "")})
    return out


def bluesky(terms: str) -> list[dict]:
    """Bluesky: open social network. Its search also says when each account was created,
    which the fake-crowd detector uses to spot a burst of brand-new accounts."""
    q = urllib.parse.quote(terms)
    data = json.loads(_get(f"https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={q}&limit=100&sort=latest"))
    out = []
    for p in data.get("posts", []):
        rec, author = p.get("record") or {}, p.get("author") or {}
        text = _clean(rec.get("text", ""))
        if not text:
            continue
        handle = author.get("handle", "")
        rkey = (p.get("uri") or "").rsplit("/", 1)[-1]
        link = ""
        for f in rec.get("facets") or []:
            for feat in f.get("features") or []:
                if feat.get("uri") and not link:
                    link = feat["uri"]
        ext = ((p.get("embed") or {}).get("external") or {})
        link = link or ext.get("uri", "")
        out.append({"title": text[:160] + ("..." if len(text) > 160 else ""), "text": text,
                    "url": f"https://bsky.app/profile/{handle}/post/{rkey}", "source": "@" + handle,
                    "platform": "Bluesky", "kind": "post", "image": ext.get("thumb", "") or "",
                    # indexedAt = when Bluesky saw it (harder to fake than the time the poster typed in)
                    "ts": _iso(p.get("indexedAt") or rec.get("createdAt", "")),
                    "author": handle, "acct_created": _iso(author.get("createdAt", "")), "link": link,
                    "link_title": _clean(ext.get("title", ""))})
    return out


SOURCES = {
    "Google News": google_news,
    "Bing News": bing_news,
    "Hacker News": hacker_news,
    "Mastodon": mastodon,
    "Bluesky": bluesky,
    "Reddit": reddit,
}


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:80]


ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _feed_time(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return 0.0
    t = _rss_time(text)
    if t:
        return t
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00").replace(" UTC", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def read_feed(url: str, limit: int = 60) -> tuple[str, list[dict]]:
    """Read any RSS or Atom feed (Google Alerts, YouTube channel, GitHub, Bluesky profile, Substack, a blog...).
    Returns (the feed's own title, items). Every item counts toward the topic: the feed already did the filtering.
    Standard terms: RSS / Atom syndication feeds."""
    root = ET.fromstring(_get(url))
    platform = feed_platform(url)
    kind = "post" if platform in ("Bluesky", "Mastodon", "Reddit") else "article"
    title = _clean(root.findtext("channel/title", "") or root.findtext(ATOM_NS + "title", ""))
    out = []
    for it in root.iter("item"):  # RSS
        link = it.findtext("link", "")
        out.append({"title": _clean(it.findtext("title", "")) or _clean(it.findtext("description", ""))[:160], "url": link,
                    "source": title or platform, "platform": platform, "kind": kind, "ts": _feed_time(it.findtext("pubDate", ""))})
    for e in root.iter(ATOM_NS + "entry"):  # Atom
        link_el = e.find(ATOM_NS + "link")
        link = link_el.get("href", "") if link_el is not None else ""
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]  # Google Alerts wraps links
        author = _clean(e.findtext(ATOM_NS + "author/" + ATOM_NS + "name", ""))
        out.append({"title": _clean(e.findtext(ATOM_NS + "title", ""))[:200], "url": real,
                    "source": urllib.parse.urlparse(real).netloc.removeprefix("www.") if platform == "Google Alerts" else (author or title or platform),
                    "platform": platform, "kind": kind,
                    "ts": _feed_time(e.findtext(ATOM_NS + "published", "") or e.findtext(ATOM_NS + "updated", ""))})
    return title, [o for o in out if o["title"] and o["ts"]][:limit]


def item_key(it: dict) -> str:
    """What counts as "the same item" when two sources return it.
    Social posts: the post's own link (so ten accounts posting the same words stay ten posts;
    the fake-crowd detector needs to see that). Articles: headline + outlet (so Google and Bing
    listing the same article count once, but the same headline at five outlets counts five times)."""
    if it.get("kind") == "post":
        return it.get("url") or norm_title(it["title"])
    t = norm_title(it["title"])
    return t and t + "|" + (it.get("source") or "").lower()[:40]


def collect_all(watch_line: str, skip: set | None = None, pool: list | None = None) -> tuple[list[dict], dict]:
    """Ask every source about one topic, plus pick matching stories from the outlet/video pool.
    Returns (items with duplicates removed, status per source)."""
    if is_feed_line(watch_line):
        url, _ = feed_parts(watch_line)
        name = feed_platform(url)
        try:
            _, items = read_feed(url)
            items.sort(key=lambda i: i["ts"], reverse=True)
            return items, {name: len(items)}
        except Exception as exc:
            return [], {name: "blocked" if "403" in str(exc) or "429" in str(exc) else "error"}
    terms = search_terms(watch_line)
    phrases = phrase_query(watch_line)
    match = matcher(watch_line)
    items, status, seen = [], {}, set()
    for name, fetch in SOURCES.items():
        if skip and name in skip:
            status[name] = "off"
            continue
        try:
            got = fetch(phrases if name in ("Google News", "Bing News") else terms)
            if name in ("Hacker News", "Mastodon", "Bluesky"):  # keep only posts that actually mention the topic
                got = [g for g in got if match(g.get("text") or g["title"])]
            status[name] = len(got)
        except Exception as exc:  # one source failing never stops the others
            status[name] = "blocked" if "403" in str(exc) or "429" in str(exc) else "error"
            continue
        for it in got:
            key = item_key(it)
            if not key or key in seen or not it["ts"]:
                continue
            seen.add(key)
            items.append(it)
    if pool is not None:  # outlets + YouTube already fetched this round
        hits = [dict(p) for p in pool if match(p["title"])]
        status["Outlets"] = sum(1 for h in hits if h["platform"] == "Outlets")
        status["YouTube"] = sum(1 for h in hits if h["platform"] == "YouTube")
        for it in hits:
            key = item_key(it)
            if key and key not in seen:
                seen.add(key)
                items.append(it)
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items, status
