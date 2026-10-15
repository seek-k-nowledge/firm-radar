"""
feeds.py - The Radar's "newsroom wall": outlets and video channels it reads every round.

Plain English: besides searching for your topics, the Radar reads the latest posts from a set of
trusted outlets, cyber-security sites, disinformation researchers and YouTube news/AI channels.
That gives it: (1) a BREAKING ticker of what just landed, (2) videos with thumbnails, and
(3) extra stories for your topics from places the search engines can miss.

Standard terms: RSS/Atom feed aggregation; a "source pool" matched against your watchlist.
Add or remove feeds in the two lists below. A feed that fails is skipped; the rest keep working.
"""

import re
import xml.etree.ElementTree as ET

from sources import _clean, _get, _rss_time

# (name, kind, url)  kind: wire = general/breaking news, tech = tech/AI, cyber = security, osint = disinfo research
OUTLETS = [
    ("NPR", "wire", "https://feeds.npr.org/1001/rss.xml"),
    ("BBC World", "wire", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "wire", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian", "wire", "https://www.theguardian.com/world/rss"),
    ("CNBC", "wire", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("The Verge", "tech", "https://www.theverge.com/rss/index.xml"),
    ("TechCrunch AI", "tech", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Ars Technica", "tech", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Wired AI", "tech", "https://www.wired.com/feed/tag/ai/latest/rss"),
    ("MIT Tech Review", "tech", "https://www.technologyreview.com/feed/"),
    ("BleepingComputer", "cyber", "https://www.bleepingcomputer.com/feed/"),
    ("The Record", "cyber", "https://therecord.media/feed"),
    ("EUvsDisinfo", "osint", "https://euvsdisinfo.eu/feed/"),
    ("Bellingcat", "osint", "https://www.bellingcat.com/feed/"),
    ("DFRLab", "osint", "https://dfrlab.org/feed/"),
]

# YouTube channels (news + AI). Channel IDs come from each channel's page.
YOUTUBE = [
    ("CNN", "UCupvZG-5ko_eiXAupbDfxWw"),
    ("Reuters", "UChqUTb7kYRX8-EiaN3XFrSQ"),
    ("Associated Press", "UC52X5wxOL_s5yw0dQk7NtgA"),
    ("BBC News", "UC16niRr50-MSBwiO3YDb3RA"),
    ("Al Jazeera English", "UCNye-wNBqNL5ZzHSJj3l8Bg"),
    ("NBC News", "UCeY0bbntWzzVIaj2z3QigXg"),
    ("CBS News", "UC8p1vwvWtl6T73JiExfWs1g"),
    ("Fox News", "UCXIJgqnII2ZOINSWNOGFThA"),
    ("DW News", "UCknLrEdhRCp1aegoMqRaCZg"),
    ("Bloomberg Television", "UCIALMKvObZNtJ6AmdCLP7Lg"),
    ("Two Minute Papers", "UCbfYPyITQ-7l4upoX8nvctg"),
    ("AI Explained", "UCNJ1Ymd5yFuUPtn21xtRbbw"),
    ("Matt Wolfe", "UChpleBmo18P08aKCIgti38g"),
]

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def _image(it) -> str:
    for tag in (MEDIA + "thumbnail", MEDIA + "content"):
        el = it.find(".//" + tag)
        if el is not None and el.get("url"):
            return el.get("url")
    enc = it.find("enclosure")
    if enc is not None and (enc.get("type", "").startswith("image") or re.search(r"\.(jpe?g|png|webp)", enc.get("url", ""), re.I)):
        return enc.get("url", "")
    desc = it.findtext("description", "") or it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', desc or "")
    return m.group(1) if m else ""


def read_feed(name: str, kind: str, url: str) -> list[dict]:
    root = ET.fromstring(_get(url))
    out = []
    for it in root.iter("item"):
        out.append({"title": _clean(it.findtext("title", "")), "url": it.findtext("link", "").strip(),
                    "source": name, "platform": "Outlets", "kind": kind, "image": _image(it),
                    "ts": _rss_time(it.findtext("pubDate", ""))})
    for e in root.iter(ATOM + "entry"):  # Atom feeds (e.g. The Verge)
        link = e.find(ATOM + "link")
        when = e.findtext(ATOM + "published", "") or e.findtext(ATOM + "updated", "")
        out.append({"title": _clean(e.findtext(ATOM + "title", "")), "url": link.get("href", "") if link is not None else "",
                    "source": name, "platform": "Outlets", "kind": kind, "image": _image(e), "ts": _iso(when)})
    return [o for o in out if o["title"] and o["ts"]]


def _iso(s: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def read_youtube(name: str, channel_id: str) -> list[dict]:
    root = ET.fromstring(_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"))
    out = []
    for e in root.iter(ATOM + "entry"):
        vid = e.findtext(YT + "videoId", "")
        group = e.find(MEDIA + "group")
        thumb = ""
        views = ""
        if group is not None:
            t = group.find(MEDIA + "thumbnail")
            thumb = t.get("url", "") if t is not None else ""
            stats = group.find(".//" + MEDIA + "statistics")
            views = stats.get("views", "") if stats is not None else ""
        out.append({"title": _clean(e.findtext(ATOM + "title", "")), "url": f"https://www.youtube.com/watch?v={vid}",
                    "source": name, "platform": "YouTube", "kind": "video",
                    "image": thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "views": views,
                    "ts": _iso(e.findtext(ATOM + "published", ""))})
    return out


def collect() -> tuple[list[dict], list[dict], dict]:
    """Returns (outlet articles, youtube videos, health per feed)."""
    arts, vids, health = [], [], {}
    for name, kind, url in OUTLETS:
        try:
            got = read_feed(name, kind, url)
            arts += got
            health[name] = len(got)
        except Exception as exc:
            health[name] = "blocked" if "403" in str(exc) or "429" in str(exc) else "error"
    for name, cid in YOUTUBE:
        try:
            got = read_youtube(name, cid)
            vids += got
            health["YouTube: " + name] = len(got)
        except Exception as exc:
            health["YouTube: " + name] = "blocked" if "403" in str(exc) or "429" in str(exc) else "error"
    arts.sort(key=lambda i: i["ts"], reverse=True)
    vids.sort(key=lambda i: i["ts"], reverse=True)
    return arts, vids, health
