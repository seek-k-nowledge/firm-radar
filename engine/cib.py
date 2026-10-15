"""
cib.py - The fake-crowd detector ("crowd check").

Plain English: the Radar already tells you THAT a story is spreading. This file looks at HOW it
is spreading, and asks: does this look like lots of different, real people reacting on their own,
or like a small group pretending to be a crowd? It looks for four warning signs:

  1. Copy-paste wording   - many different accounts posting nearly the same words, close together
  2. Brand-new accounts   - a big share of the posts come from accounts made in the last 30 days
                            (compared with how many new accounts are normal this round)
  3. One link hammered    - lots of accounts pushing the same outside link within an hour
  4. A few loud accounts  - a handful of accounts doing most of the talking

Each warning sign adds points. 0 points = green (looks organic). 1-2 = amber (worth a look).
3+ = red (looks staged). Fewer than 5 social posts = grey (too few to judge).

IMPORTANT: this finds PATTERNS, not proof. Red means "look closer", not "this is an operation".
News wires, fan groups, and people sharing one viral link can all look coordinated. The check
lists what would explain each sign innocently, so you can rule those out before you tell a client.

Standard terms: coordinated inauthentic behavior (CIB) detection. The wording check is
"near-duplicate detection" using word shingles and Jaccard similarity (how much two texts overlap).
The new-account check compares against a "baseline" (what's normal across everything this round).
"""

import re
import statistics
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SOCIAL = {"Bluesky", "Mastodon", "Reddit", "Hacker News"}
WIRES = re.compile(r"associated press|\bap\b|reuters|afp|agence france|bloomberg|yahoo|msn|"
                   r"aol|newsbreak|upi|cnn wire|dpa|press association", re.I)
STOP = set("""the a an and or of to in on for is are was were be been by with at from as that this
it its into about after over new says said will has have had not but just more than so if what who
how why when they their them we our you your he she his her i me my rt via amp""".split())

FEED_RE = re.compile(r"(bot|feed|rss|digest|brief|curated|hacker|h4ck|betterhn|hnws|^hn|hn\d|headline|aggregat|newswire|alerts?$|daily$|top\d|news\d)", re.I)
NEW_DAYS = 30            # an account younger than this counts as "brand-new"
MIN_SOCIAL = 5           # fewer social posts than this = too few to judge
SIMILAR = 0.5            # two posts sharing at least half their 3-word phrases = "nearly the same words"


def _words(text: str) -> list[str]:
    text = re.sub(r"https?://\S+|\b\S+\.(com|org|net|io|co|ly|app|social)/\S*", " ", (text or "").lower())
    return [w for w in re.findall(r"[a-z0-9']+", text) if w not in STOP and len(w) > 1]


def _shingles(ws: list[str], n: int = 3) -> set:
    if len(ws) < n:
        return {tuple(ws)} if ws else set()
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def norm_link(url: str) -> str:
    """Same link, written differently, should count as one: drop tracking bits, www, trailing slash."""
    if not url:
        return ""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return ""
    host = (u.netloc or "").lower().removeprefix("www.")
    if not host or host in ("bsky.app", "mastodon.social", "reddit.com", "news.ycombinator.com"):
        return ""
    q = urlencode([(k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref"))])
    return urlunparse(("https", host, u.path.rstrip("/"), "", q, ""))


def _ev(it: dict) -> dict:
    return {"title": it.get("title", ""), "url": it.get("url", ""), "who": it.get("author") or it.get("source", ""),
            "platform": it.get("platform", ""), "ts": it.get("ts", 0)}


def _clusters(items: list[dict]) -> list[list[dict]]:
    """Group items whose wording nearly matches (union-find over pairs)."""
    sh = [_shingles(_words(it.get("text") or it.get("title", ""))) for it in items]
    ok = [len(s) >= 4 for s in sh]  # very short posts ("wow", "this!") would match by accident
    parent = list(range(len(items)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        if not ok[i]:
            continue
        for j in range(i + 1, len(items)):
            if ok[j] and _jaccard(sh[i], sh[j]) >= SIMILAR:
                parent[root(i)] = root(j)
    groups = {}
    for i in range(len(items)):
        groups.setdefault(root(i), []).append(items[i])
    return [g for g in groups.values() if len(g) > 1]


def is_feed(it: dict) -> bool:
    """An account whose name says it's an automated news feed (hnbot, techbriefly, curatedhackernews...).
    These repost headlines all day. They're automation, but they aren't pretending to be people."""
    return bool(FEED_RE.search(operator(it)))


def operator(it: dict) -> str:
    """Who is behind a post, as best we can tell from the outside. One person often runs the same name on
    several sites (rom.bsky.social, rom.social, rom.brid.gy...), so those count as one."""
    h = (it.get("author") or it.get("source") or "").lower().lstrip("@")
    return re.split(r"[.@]", h, 1)[0] or h


def family(group: list[dict]):
    """Several accounts whose names share a long ending (uspoliticsbriefly, leftwingbriefly...) = likely one operator."""
    names = {operator(i) for i in group if i.get("author")}
    best = None
    for n in names:
        for k in range(len(n) - 5):
            suf = n[k:]
            if len(suf) < 6:
                break
            members = {(i.get("author") or "").lower() for i in group if operator(i).endswith(suf) and operator(i) != suf}
            members |= {(i.get("author") or "").lower() for i in group if operator(i) == suf}
            if len({operator(i) for i in group if (i.get("author") or "").lower() in members}) >= 3 and (not best or len(suf) > len(best[0])):
                best = (suf, members)
    return best


def is_headline_share(it: dict, heads: list[list[str]]) -> bool:
    """True if a post is just a news headline being shared: its words mostly match the title of the
    link it carries, or a headline the Radar found from a news outlet on this topic."""
    words = _words(it.get("text") or it.get("title", ""))[:40]
    if not words:
        return False
    ws = set(words)
    cands = list(heads)
    if it.get("link_title"):
        cands.append(_words(it["link_title"]))
    for h in cands:
        if len(h) >= 5 and sum(w in ws for w in h) / len(h) >= 0.75:
            return True
    return False


def _short(it: dict, n: int = 70) -> str:
    t = it.get("text") or it.get("title", "")
    return t[:n] + ("…" if len(t) > n else "")


def new_account_share(items: list[dict]) -> tuple[int, int]:
    """(posts by brand-new accounts, posts where we know the account's age)."""
    known = new = 0
    for it in items:
        made = it.get("acct_created") or 0
        if it.get("platform") in SOCIAL and made and it.get("ts"):
            known += 1
            new += (it["ts"] - made) < NEW_DAYS * 86400
    return new, known


def baseline(all_items: list[dict]) -> float:
    """Share of posts from brand-new accounts across EVERYTHING the Radar saw this round.
    A topic only gets flagged if it's well above this normal level."""
    new, known = new_account_share(all_items)
    return new / known if known >= 20 else 0.15


def check(items: list[dict], now: float, base_new: float = 0.15) -> dict:
    """Run the crowd check on one topic's items. Returns a dict the Radar page and reports use."""
    day = [it for it in items if it.get("ts") and now - it["ts"] <= 86400]
    social = [it for it in day if it.get("platform") in SOCIAL]
    articles = [it for it in day if it.get("platform") not in SOCIAL]
    authors = {operator(it) for it in social}
    flags, organic, notes, pts = [], [], [], 0

    # 1. Copy-paste wording among social posts.
    #    Not counted: people or news-bot accounts posting a real news headline (that's how news spreads),
    #    and one person cross-posting the same words to several of their own accounts.
    heads = [_words(a.get("title", "")) for a in articles + [i for i in social if i.get("platform") == "Hacker News"]]
    for g in sorted(_clusters(social), key=len, reverse=True)[:4]:
        span = (max(i["ts"] for i in g) - min(i["ts"] for i in g)) / 3600
        shares = [i for i in g if is_headline_share(i, heads) or is_feed(i)]
        rest = [i for i in g if i not in shares]
        ops = {operator(i) for i in rest}
        fam = family(g)
        if fam and all(is_feed(i) for i in g if (i.get("author") or "").lower() in fam[1]):
            notes.append(f"{len(fam[1])} automated news-feed accounts (names ending in \"{fam[0]}\") posted the same summary. "
                         "That's a news-bot service, not people pretending to be a crowd.")
            fam = None
        if len({operator(i) for i in g}) >= 3 and len(shares) >= len(g) / 2:
            organic.append(f"{len({operator(i) for i in shares})} accounts (people and news-feed bots) posted the same news headline "
                           f"(\"{_short(shares[0])}\"). That's normal: people and news-bot accounts share headlines word for word.")
        if fam and len(fam[1]) >= 3:
            pts += 1
            flags.append({"kind": "account_family", "pts": 1,
                          "title": f"{len(fam[1])} look-alike accounts (names ending in \"{fam[0]}\") posted the same words",
                          "innocent": "A news-bot service running a set of themed accounts. Open the profiles: honest ones say they're automated.",
                          "evidence": [_ev(i) for i in g if (i.get("author") or "").lower() in fam[1]][:6]})
            continue
        if len(ops) < 3 or span > 6:
            continue
        p = 2 if len(ops) >= 5 and span <= 2 else 1
        pts += p
        flags.append({"kind": "copy_paste", "pts": p,
                      "title": f"{len(ops)} different accounts posted nearly the same words within {span:.1f} hours",
                      "innocent": "A shared quote, a petition or 'share this' template, or fans copying a post they liked.",
                      "evidence": [_ev(i) for i in sorted(rest, key=lambda x: x["ts"])[:6]]})

    # 1b. Same headline at many outlets: almost always a news wire (AP, Reuters) story. Shown as a note, no points.
    for g in sorted(_clusters(articles), key=len, reverse=True)[:1]:
        outlets = {(i.get("source") or "").strip() for i in g if i.get("source")}
        if len(outlets) >= 4:
            notes.append(f"The same headline ran at {len(outlets)} outlets (\"{_short(g[0])}\"). That's almost always a news-wire story. "
                         "Only worth a look if the outlets are sites you've never heard of; networks of look-alike local "
                         "'news' sites (pink-slime sites) are a known trick.")

    # 2. Brand-new accounts, compared with what's normal this round
    new, known = new_account_share(social)
    share = new / known if known else 0.0
    if known >= MIN_SOCIAL and new >= 3 and share >= max(0.4, 2 * base_new):
        p = 2 if share >= 0.6 and new >= 5 else 1
        pts += p
        newest = sorted([i for i in social if i.get("acct_created") and i["ts"] - i["acct_created"] < NEW_DAYS * 86400],
                        key=lambda i: -i["acct_created"])
        flags.append({"kind": "new_accounts", "pts": p,
                      "title": f"{new} of {known} posts ({share:.0%}) came from accounts less than {NEW_DAYS} days old "
                               f"(normal right now: about {base_new:.0%})",
                      "innocent": "A wave of new sign-ups to the platform (it happens when people leave another site), "
                                  "or a new community forming around this exact issue.",
                      "evidence": [dict(_ev(i), age_days=round((i["ts"] - i["acct_created"]) / 86400, 1)) for i in newest[:6]]})
    elif known >= MIN_SOCIAL and share <= max(0.25, base_new * 1.2):
        organic.append(f"Most accounts are established: only {share:.0%} are under {NEW_DAYS} days old (normal: about {base_new:.0%}).")

    # 3. One link hammered
    by_link = {}
    feed_links = {}
    for it in social:
        nl = norm_link(it.get("link", ""))
        if nl and is_feed(it):
            feed_links.setdefault(nl, set()).add(operator(it))
        elif nl:
            by_link.setdefault(nl, []).append(it)
    for link, who in sorted(feed_links.items(), key=lambda kv: -len(kv[1]))[:1]:
        if len(who) >= 3:
            notes.append(f"{len(who)} automated news-feed accounts reposted the same link ({link.replace('https://', '')[:60]}). "
                         "Feed bots do this with every popular story.")
    for link, posts in sorted(by_link.items(), key=lambda kv: -len(kv[1]))[:2]:
        posts.sort(key=lambda i: i["ts"])
        best, best_who = [], set()
        for i in range(len(posts)):  # most distinct accounts sharing it inside any 1-hour window
            win = [x for x in posts[i:] if x["ts"] - posts[i]["ts"] <= 3600]
            who = {operator(x) for x in win}
            if len(who) > len(best_who):
                best, best_who = win, who
        if len(best_who) >= 5:
            plain = sum(is_headline_share(x, heads) for x in best) >= len(best) / 2
            p = 1 if plain and len(best_who) < 10 else 2
            pts += p
            flags.append({"kind": "link_push", "pts": p,
                          "title": f"{len(best_who)} accounts shared the same link within one hour: {link[:90]}",
                          "innocent": "A genuinely viral article, or a big account's followers all re-sharing it.",
                          "evidence": [_ev(i) for i in best[:6]]})
        elif len({operator(x) for x in posts}) >= 6 and len(posts) >= 0.5 * len(social):
            pts += 1
            flags.append({"kind": "link_push", "pts": 1,
                          "title": f"Half the conversation is one link, shared by {len(posts)} posts: {link[:90]}",
                          "innocent": "The story may simply be that one article.",
                          "evidence": [_ev(i) for i in posts[:6]]})

    # 4. A few loud accounts
    if len(social) >= 8:
        counts = {}
        for it in social:
            k = operator(it)
            counts[k] = counts.get(k, 0) + 1
        top, n = max(counts.items(), key=lambda kv: kv[1])
        if n >= 4 and n / len(social) >= 0.35:
            pts += 1
            flags.append({"kind": "loud_few", "pts": 1,
                          "title": f"One account ({top}) made {n} of {len(social)} posts ({n / len(social):.0%})",
                          "innocent": "A reporter live-posting, a news bot, or the person at the center of the story.",
                          "evidence": [_ev(i) for i in social if operator(i) == top][:6]})
        elif len(authors) / len(social) >= 0.8:
            organic.append(f"No single account dominates: {len(authors)} accounts for {len(social)} posts.")

    plats = {it.get("platform") for it in day}
    if len(plats) >= 3:
        organic.append(f"It's showing up on {len(plats)} different platforms, not just one.")
    if not flags and social and len(social) >= MIN_SOCIAL:
        organic.append("No copy-paste wording, no single link being pushed.")

    if len(social) < MIN_SOCIAL and not flags:
        level, label = "thin", "TOO FEW POSTS"
        summary = (f"Only {len(social)} social post{'s' if len(social) != 1 else ''} in the last 24 hours; the check needs at least "
                   f"{MIN_SOCIAL} to say anything. This topic is mostly news articles right now.")
    elif pts == 0:
        level, label = "organic", "LOOKS ORGANIC"
        summary = "Looks like real, varied people. None of the four warning signs showed up."
    elif pts <= 2:
        level, label = "mixed", "WORTH A LOOK"
        summary = "One warning sign showed up. Check it against the innocent explanation before reading anything into it."
    else:
        level, label = "staged", "LOOKS STAGED"
        summary = "Several warning signs at once. Open the evidence, check the accounts by hand, then decide."

    n = len(social)
    confidence = ("Low" if n < 15 else "Moderate") if level != "thin" else "None"
    return {"level": level, "label": label, "score": pts, "summary": summary, "flags": flags, "organic": organic, "notes": notes,
            "social_posts": n, "accounts": len(authors), "articles": len(articles),
            "new_share": round(share, 3), "new_known": known, "baseline_new": round(base_new, 3),
            "confidence": confidence,
            "confidence_why": ("Automated pattern check on public posts only. It can't see private groups, deleted posts, "
                               "or who runs an account, so it never rates itself High. Checking the accounts by hand can raise it.")}


def graph(items: list[dict], now: float, max_accounts: int = 70) -> dict:
    """The data behind the Network view: who is connected to whom.

    Plain English: every social account in the last 24 hours becomes a dot. Two kinds of "hubs" tie
    dots together: a MESSAGE hub (several accounts posted nearly the same words) and a LINK hub
    (several accounts shared the same outside link). A real conversation looks like a loose cloud of
    separate dots. A coordinated push looks like a tight star: many young accounts all wired to the
    same message and the same link.

    Standard term: a bipartite network graph (accounts on one side, content on the other).
    """
    day = [it for it in items if it.get("ts") and now - it["ts"] <= 86400 and it.get("platform") in SOCIAL]
    heads = [_words(a.get("title", "")) for a in items if a.get("platform") not in SOCIAL or a.get("platform") == "Hacker News"]
    accounts = {}
    for it in day:
        k = operator(it)
        if not k:
            continue
        a = accounts.setdefault(k, {"id": "a:" + k, "type": "account", "label": it.get("author") or it.get("source") or k,
                                    "platform": it.get("platform"), "posts": 0, "age_days": None, "first": it["ts"],
                                    "feed": is_feed(it), "url": it.get("url", "")})
        a["posts"] += 1
        a["first"] = min(a["first"], it["ts"])
        if it.get("acct_created"):
            age = round((it["ts"] - it["acct_created"]) / 86400, 1)
            a["age_days"] = age if a["age_days"] is None else min(a["age_days"], age)
    hubs, edges = [], []
    for n, g in enumerate(sorted(_clusters(day), key=len, reverse=True)[:12]):
        who = {operator(i) for i in g}
        if len(who) < 2:
            continue
        shared = sum(is_headline_share(i, heads) for i in g) >= len(g) / 2
        hid = f"m:{n}"
        txt = g[0].get("text") or g[0].get("title", "")
        hubs.append({"id": hid, "type": "message", "label": txt[:90] + ("…" if len(txt) > 90 else ""),
                     "size": len(who), "headline": shared, "span_h": round((max(i["ts"] for i in g) - min(i["ts"] for i in g)) / 3600, 1)})
        edges += [{"s": "a:" + w, "t": hid, "kind": "same_words"} for w in who if w]
    by_link = {}
    for it in day:
        nl = norm_link(it.get("link", ""))
        if nl:
            by_link.setdefault(nl, set()).add(operator(it))
    for n, (link, who) in enumerate(sorted(by_link.items(), key=lambda kv: -len(kv[1]))[:12]):
        if len(who) < 2:
            continue
        hid = f"l:{n}"
        hubs.append({"id": hid, "type": "link", "label": link.replace("https://", "")[:70], "url": link, "size": len(who)})
        edges += [{"s": "a:" + w, "t": hid, "kind": "shared_link"} for w in who if w]
    wired = {e["s"] for e in edges}
    keep = sorted(accounts.values(), key=lambda a: (a["id"] not in wired, -a["posts"]))[:max_accounts]
    ids = {a["id"] for a in keep}
    for a in keep:
        a["new"] = a["age_days"] is not None and a["age_days"] < NEW_DAYS
    edges = [e for e in edges if e["s"] in ids]
    return {"nodes": keep + hubs, "edges": edges, "accounts_total": len(accounts)}
