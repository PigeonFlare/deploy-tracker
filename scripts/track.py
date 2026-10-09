#!/usr/bin/env python3
"""Collect the most-upvoted website launches from Show HN and Reddit, keep each launch
month's top 100, and check every tracked site once a day.

Posts come from three places, merged by post URL:
  * deploy-list's published rankings (the last 30 days, with live Reddit vote counts),
  * hn.algolia.com for Show HN, back to START,
  * the Arctic Shift Reddit archive, read a little each run until it reaches the present.

The same site posted more than once counts once: its first post is the launch, and it
keeps the highest vote count it got. Standard library only.
"""

import argparse
import base64
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

sys.path.insert(0, os.path.dirname(__file__))
from rules import (MAKER_TITLE, NSFW, URL_RE, blocked, category, host_of, is_ip,  # noqa: E402
                   page_summary, pick_site, public_host, site_url)

ROOT = os.path.join(os.path.dirname(__file__), "..")
POSTS = os.path.join(ROOT, "data", "posts.json")
STATE = os.path.join(ROOT, "data", "state.json")
OUT = os.path.join(ROOT, "site", "data", "tracker.json")
WAYBACK = os.path.join(ROOT, "data", "wayback.json")

UA = "deploy-tracker/1.0 (+https://github.com/PigeonFlare/deploy-tracker)"
START = dt.datetime(2025, 10, 1, tzinfo=dt.timezone.utc)
TOP_N = 100
MIN_VOTES = 10
DEPLOY_LIST_FEED = "https://raw.githubusercontent.com/PigeonFlare/deploy-list/main/site/data/sites.json"
SUBREDDITS = ["SideProject", "InternetIsBeautiful", "IMadeThis", "ClaudeAI", "SaaS"]
MAKER_ONLY = {"ClaudeAI", "SaaS"}
SOURCES = [{"name": "Show HN", "url": "https://news.ycombinator.com/show"}] + [
    {"name": f"r/{s}", "url": f"https://www.reddit.com/r/{s}/"} for s in SUBREDDITS]

ARCHIVE_API = "https://arctic-shift.photon-reddit.com/api/posts"
ARCHIVE_BUDGET = 600
ARCHIVE_MINUTES = 18
ARCHIVE_STRIKES = 6
ARCHIVE_PAUSE = 2
ARCHIVE_SETTLE = 36 * 3600  # the archive re-reads a post's score about 36 hours after it's posted
ARCHIVE_FIELDS = "id,title,url,score,created_utc,over_18"

FREE_HOSTS = ("vercel.app", "netlify.app", "github.io", "pages.dev", "workers.dev", "onrender.com",
              "herokuapp.com", "fly.dev", "replit.app", "repl.co", "glitch.me", "web.app", "firebaseapp.com",
              "surge.sh", "railway.app", "up.railway.app", "lovable.app", "bolt.host", "netlify.com",
              "streamlit.app", "hf.space", "framer.website", "framer.app", "webflow.io", "carrd.co",
              "gitlab.io", "deno.dev", "azurewebsites.net", "amplifyapp.com", "codeberg.page", "neocities.org")

PARKED_HOSTS = ("sedo.com", "dan.com", "afternic.com", "hugedomains.com", "bodis.com", "parkingcrew.net",
                "above.com", "undeveloped.com", "sav.com", "squadhelp.com", "atom.com", "brandbucket.com",
                "parkingpage.namecheap.com", "domainmarket.com", "buydomains.com", "uniregistry.com", "efty.com")
PARKED_TEXT = re.compile(
    r"domain(?: name)? (?:is|may be) for sale|buy this domain|this domain(?: name)? (?:is|has been) (?:parked|registered|for sale)|"
    r"parked (?:free|domain)|domain parking|available for purchase|make an offer on this domain|"
    r"sedoparking|parkingcrew|bodis\.com|window\.location\.href\s*=\s*[\"']/lander", re.I)
GONE_TEXT = re.compile(
    r"deployment (?:has been|is) (?:disabled|paused|not found)|DEPLOYMENT_NOT_FOUND|DEPLOYMENT_DISABLED|"
    r"\b(?:site|project|application) not found\b|no such app|there isn't a github pages site here|"
    r"this (?:site|app|service|project|account) (?:has been|is) (?:suspended|paused|disabled|deleted)|"
    r"\baccount (?:has been )?suspended\b", re.I)

UP, WALLED, PARKED, DOWN = "U", "B", "P", "D"
WAYBACK_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BUDGET = 300
WAYBACK_MINUTES = 8
WAYBACK_REFRESH = 14  # days before a site's archive history is looked up again
PROBE_DEADLINE = 30
PROBE_HOPS = 6


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def fetch(url, timeout=30, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(30_000_000)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data, pretty=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(data, f, indent=1, ensure_ascii=False)
        else:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def add_post(posts, post_url, url, title, votes, source, created, cat=None):
    if not url or not isinstance(votes, int) or not created or created < START.timestamp() or NSFW.search(title or ""):
        return False
    old = posts.get(post_url)
    entry = {"url": url, "title": title, "votes": max(votes, old["votes"]) if old else votes,
             "source": source, "created": int(created)}
    cat = cat or (old or {}).get("category")
    if cat:
        entry["category"] = cat
    posts[post_url] = entry
    return old is None


# --- Collection ---------------------------------------------------------------------

def collect_deploy_list(posts):
    data = json.loads(fetch(DEPLOY_LIST_FEED))
    new = 0
    for s in data.get("sites") or []:
        source = "Show HN" if s.get("source") == "Hacker News" else s.get("source")
        new += add_post(posts, s["post_url"], s.get("url"), s.get("title") or "", s.get("votes"),
                        source, s.get("created"), s.get("category"))
    print(f"deploy-list: {len(data.get('sites') or [])} sites, {new} new posts")


def week_chunks(start, end):
    t = start
    while t < end:
        yield t, min(t + dt.timedelta(days=7), end)
        t += dt.timedelta(days=7)


def reddit_token():
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return None
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = fetch("https://www.reddit.com/api/v1/access_token", headers={"Authorization": f"Basic {auth}"},
                 data=b"grant_type=client_credentials")
    return json.loads(body)["access_token"]


def collect_reddit_api(posts, state, now):
    """With Reddit API credentials, read each subreddit's top posts of the year (up to 1,000,
    once a week) and of the month (every run), with exact vote counts."""
    token = reddit_token()
    if not token:
        print("Reddit API: no credentials; using the archive")
        return False
    year_due = now.timestamp() - state.get("reddit_year_at", 0) > 7 * 86400
    new = 0
    for sub in SUBREDDITS:
        after = ""
        for page in range(10 if year_due else 1):
            q = urllib.parse.urlencode({"t": "year" if year_due else "month", "limit": 100, "after": after, "raw_json": 1})
            try:
                data = json.loads(fetch(f"https://oauth.reddit.com/r/{sub}/top?{q}", headers={"Authorization": f"bearer {token}"}))["data"]
            except Exception as e:
                print(f"warn: r/{sub} API: {e}", file=sys.stderr)
                break
            for c in data["children"]:
                p = c["data"]
                title = p.get("title") or ""
                if p.get("over_18") or p.get("stickied") or (sub in MAKER_ONLY and not MAKER_TITLE.search(title)):
                    continue
                direct = None if p.get("is_self") else p.get("url_overridden_by_dest") or p.get("url")
                url = pick_site(title, direct, URL_RE.findall(p.get("selftext") or ""))
                new += add_post(posts, f"https://www.reddit.com{p.get('permalink', '')}", url, title,
                                p.get("score"), f"r/{sub}", p.get("created_utc"))
            after = data.get("after")
            if not after:
                break
            time.sleep(1)
    if year_due:
        state["reddit_year_at"] = int(now.timestamp())
    print(f"Reddit API: {new} new posts" + (" (year backfill)" if year_due else ""))
    return True


def collect_hn(posts, state, now):
    """Read Show HN a week at a time. A week is read again until it's two weeks old, so its
    points settle; after that it's done."""
    done = set(state.setdefault("hn_weeks", []))
    new = reads = 0
    for a, b in week_chunks(START, now):
        key = a.date().isoformat()
        if key in done:
            continue
        q = urllib.parse.urlencode({
            "tags": "show_hn", "hitsPerPage": 1000,
            "numericFilters": f"created_at_i>={int(a.timestamp())},created_at_i<{int(b.timestamp())},points>={MIN_VOTES}",
        })
        try:
            hits = json.loads(fetch(f"https://hn.algolia.com/api/v1/search_by_date?{q}"))["hits"]
        except Exception as e:
            print(f"warn: Show HN week of {key}: {e}", file=sys.stderr)
            continue
        reads += 1
        for h in hits:
            title = re.sub(r"^show hn:\s*", "", h.get("title") or "", flags=re.I)
            new += add_post(posts, f"https://news.ycombinator.com/item?id={h['objectID']}",
                            site_url(h.get("url") or "", title), title, h.get("points"), "Show HN", h.get("created_at_i"))
        if now - b > dt.timedelta(days=14):
            done.add(key)
    state["hn_weeks"] = sorted(done)
    print(f"Show HN: {reads} weeks read, {new} new posts")


class ArchivePaused(Exception):
    pass


def archive_get(params, budget, path="search"):
    if budget[0] <= 0:
        raise ArchivePaused("request budget spent")
    budget[0] -= 1
    time.sleep(ARCHIVE_PAUSE)
    try:
        data = json.loads(fetch(f"{ARCHIVE_API}/{path}?{urllib.parse.urlencode(params)}", timeout=60))
    except urllib.error.HTTPError as e:
        if e.code in (422, 429, 503):
            raise ArchivePaused(f"HTTP {e.code}")
        raise
    if data.get("error"):
        raise ArchivePaused(str(data["error"]))
    return data.get("data") or []


def collect_reddit(posts, state, now, budget=ARCHIVE_BUDGET, minutes=ARCHIVE_MINUTES):
    """Walk each subreddit forward from START through the archive, oldest first, stopping
    ARCHIVE_SETTLE short of now so the scores read are settled. Subreddits take turns, one
    request each, and the cursors are saved, so a run that spends its budget picks up where
    it left off."""
    cursors = state.setdefault("reddit_cursors", {})
    left = [budget]
    new = 0
    limit = "auto"
    stop = int(now.timestamp() - ARCHIVE_SETTLE)
    deadline = time.monotonic() + minutes * 60
    strikes = 0
    pending = [sub for sub in SUBREDDITS if cursors.get(sub, int(START.timestamp())) < stop]
    try:
        while pending and time.monotonic() < deadline:
            for sub in list(pending):
                after = cursors.get(sub, int(START.timestamp()))
                params = {"subreddit": sub, "after": after, "before": stop, "sort": "asc", "fields": ARCHIVE_FIELDS}
                try:
                    while True:
                        try:
                            batch = archive_get({**params, "limit": limit}, left)
                            break
                        except ArchivePaused as e:
                            strikes += 1
                            if left[0] <= 0 or strikes > ARCHIVE_STRIKES:
                                raise
                            limit = 100
                            print(f"Reddit archive: {e}; waiting {30 * strikes}s")
                            time.sleep(30 * strikes)
                    strikes = 0
                except urllib.error.HTTPError as e:
                    if e.code == 400 and limit != 100:
                        limit = 100
                        continue
                    print(f"warn: r/{sub} archive: {e}", file=sys.stderr)
                    pending.remove(sub)
                    continue
                keep = [p for p in batch if not p.get("over_18") and isinstance(p.get("score"), int)
                        and p["score"] >= MIN_VOTES
                        and not (sub in MAKER_ONLY and not MAKER_TITLE.search(p.get("title") or ""))]
                text = [p["id"] for p in keep if not site_url(p.get("url") or "", p.get("title") or "")]
                bodies = {}
                for i in range(0, len(text), 100):
                    for t in archive_get({"ids": ",".join(text[i:i + 100]), "fields": "id,selftext"}, left, "ids"):
                        bodies[t.get("id")] = t.get("selftext") or ""
                for p in keep:
                    title = p.get("title") or ""
                    url = pick_site(title, p.get("url"), URL_RE.findall(bodies.get(p["id"], "")))
                    new += add_post(posts, f"https://www.reddit.com/r/{sub}/comments/{p['id']}/", url, title,
                                    p["score"], f"r/{sub}", p.get("created_utc"))
                if not batch:
                    cursors[sub] = stop
                    pending.remove(sub)
                else:
                    cursors[sub] = int(batch[-1]["created_utc"]) + 1
    except ArchivePaused as e:
        print(f"Reddit archive: pausing until the next run ({e})")
    except Exception as e:
        print(f"warn: Reddit archive: {e}", file=sys.stderr)
    behind = {s: dt.datetime.fromtimestamp(cursors.get(s, START.timestamp()), dt.timezone.utc).date().isoformat()
              for s in SUBREDDITS}
    print(f"Reddit archive: {budget - left[0]} requests, {new} new posts; read up to {behind}")


# --- Ranking ------------------------------------------------------------------------

def site_key(url):
    parts = urllib.parse.urlsplit(url)
    path = re.sub(r"/(index\.html?)?$", "", parts.path or "")
    return host_of(url) + path.lower()


def hosting(domain):
    for h in FREE_HOSTS:
        if domain == h or domain.endswith("." + h):
            return h
    return None


def rank_sites(posts):
    """One entry per site, its launch month and its rank in that month by votes."""
    sites = {}
    for post_url, p in sorted(posts.items(), key=lambda kv: kv[1]["created"]):
        key = site_key(p["url"])
        s = sites.get(key)
        if s is None:
            sites[key] = s = {"id": key, "url": p["url"], "domain": host_of(p["url"]), "title": p["title"],
                              "votes": p["votes"], "source": p["source"], "post_url": post_url,
                              "created": p["created"], "sources": [p["source"]]}
        else:
            if p["source"] not in s["sources"]:
                s["sources"].append(p["source"])
            if p["votes"] > s["votes"]:
                s.update(votes=p["votes"], title=p["title"], post_url=post_url, source=p["source"])
        if p.get("category"):
            s["category"] = p["category"]
    cohorts = {}
    for s in sites.values():
        s["cohort"] = dt.datetime.fromtimestamp(s["created"], dt.timezone.utc).strftime("%Y-%m")
        if s["votes"] >= MIN_VOTES:
            cohorts.setdefault(s["cohort"], []).append(s)
    for members in cohorts.values():
        members.sort(key=lambda s: (-s["votes"], s["created"]))
        for i, s in enumerate(members, 1):
            s["rank"] = i
    return [s for s in sites.values() if "rank" in s]


# --- Checking -----------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _reason(exc):
    text = str(getattr(exc, "reason", exc)).lower()
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in text:
        return "timeout"
    if isinstance(getattr(exc, "reason", None), ssl.SSLError) or "ssl" in text or "certificate" in text:
        return "tls"
    if "refused" in text or "reset" in text:
        return "refused"
    return "connection"


def probe(url):
    """(status char, details). Up means the landing page loads; a bot wall (401/403/429 or a
    Cloudflare challenge) counts as up, since a person gets through. Parked means the domain
    now shows a for-sale or parking page."""
    deadline = time.monotonic() + PROBE_DEADLINE
    started = time.monotonic()
    for _ in range(PROBE_HOPS + 1):
        host = host_of(url)
        if not url.startswith(("http://", "https://")) or not host or is_ip(host):
            return DOWN, {"reason": "bad url"}
        if any(host == h or host.endswith("." + h) for h in PARKED_HOSTS):
            return PARKED, {"reason": "parked", "final": host}
        if time.monotonic() > deadline:
            return DOWN, {"reason": "timeout"}
        public = public_host(host)
        if public is None:
            return DOWN, {"reason": "dns"}
        if not public:
            return DOWN, {"reason": "private address"}
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36 deploy-tracker",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.8"})
        try:
            resp = _OPENER.open(req, timeout=15)
        except urllib.error.HTTPError as e:
            code, headers = e.code, e.headers
            location = headers.get("Location") if code in (301, 302, 303, 307, 308) else None
            body = b""
            if not location:
                try:
                    body = e.read(200_000)
                except Exception:
                    pass
            e.close()
            if location:
                url = urllib.parse.urljoin(url, location)
                continue
            ms = int((time.monotonic() - started) * 1000)
            if headers.get("cf-mitigated") or code in (401, 403, 429):
                return WALLED, {"code": code, "ms": ms, "final": host}
            if code == 503 and b"challenge-platform" in body:
                return WALLED, {"code": code, "ms": ms, "final": host}
            reason = "http 404" if code in (404, 410) else "http 5xx" if code >= 500 else f"http {code}"
            if GONE_TEXT.search(body[:20_000].decode("utf-8", "replace")):
                reason = "removed"
            return (DOWN if code in (404, 410) or code >= 500 else UP), {"code": code, "ms": ms, "reason": reason, "final": host}
        except Exception as e:
            return DOWN, {"reason": _reason(e)}
        with resp:
            ms = int((time.monotonic() - started) * 1000)
            try:
                body = resp.read(300_000)
            except Exception:
                body = b""
            info = {"code": resp.status, "ms": ms, "final": host}
            text = body.decode("utf-8", "replace")
            if urllib.parse.urlsplit(url).path.rstrip("/") == "/lander" or PARKED_TEXT.search(text[:60_000]):
                return PARKED, {**info, "reason": "parked"}
            if len(body) < 30_000 and GONE_TEXT.search(text):
                return DOWN, {**info, "reason": "removed"}
            if "html" in (resp.headers.get("Content-Type") or "").lower():
                info["summary"] = page_summary(body)
                info["fp"] = fingerprint(text)
            info["lm"] = last_modified(resp.headers.get("Last-Modified"))
            return UP, info
    return DOWN, {"reason": "redirect loop"}


_STRIP = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_ASSET = re.compile(r"""<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)


def fingerprint(text):
    """A short hash of what a visitor would notice changing: the page's words (minus digits, so
    clocks and counters don't count) and the bundled scripts and styles it loads, whose file
    names usually change on every deploy."""
    assets = sorted({a for a in _ASSET.findall(text) if not re.search(r"[?&](v|t|ts|_)=\d{9,}", a)})
    words = re.sub(r"<[^>]+>", " ", _STRIP.sub(" ", text))
    words = re.sub(r"\s+", " ", re.sub(r"\d+", "", html.unescape(words))).strip()
    return hashlib.sha1("\n".join(assets + [words]).encode()).hexdigest()[:12]


def last_modified(value, now=None):
    """Last-Modified as a timestamp, ignoring servers that just send the current time."""
    if not value:
        return None
    try:
        ts = int(email.utils.parsedate_to_datetime(value).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None
    now = now or time.time()
    return ts if START.timestamp() - 400 * 86400 < ts < now - 3600 else None


def wayback_changes(url, since):
    """Timestamps at which the Internet Archive saw the page's content change."""
    target = re.sub(r"^https?://", "", url).rstrip("/") + "/"
    q = urllib.parse.urlencode({"url": target, "output": "json", "fl": "timestamp", "filter": "statuscode:200",
                                "collapse": "digest", "from": time.strftime("%Y%m%d", time.gmtime(since)), "limit": 5000})
    rows = json.loads(fetch(f"{WAYBACK_API}?{q}", timeout=40) or b"[]")
    out = []
    for row in rows[1:]:
        try:
            out.append(int(dt.datetime.strptime(row[0][:14], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()))
        except (ValueError, IndexError):
            pass
    return out


def update_wayback(sites, archive, today, budget=WAYBACK_BUDGET, minutes=WAYBACK_MINUTES, pause=1.5, strikes=5):
    """Look up archive history for sites never looked up or not refreshed lately, one at a time,
    backing off when the Archive says it's busy."""
    def stale(s):
        seen = archive.get(s["id"], {}).get("t")
        return not seen or (dt.date.fromisoformat(today) - dt.date.fromisoformat(seen)).days >= WAYBACK_REFRESH
    queue = sorted((s for s in sites if stale(s)), key=lambda s: (s["id"] in archive, archive.get(s["id"], {}).get("t", "")))
    queue = queue[:budget]
    stop = time.monotonic() + minutes * 60
    errors, done, busy, i = {}, 0, 0, 0
    while i < len(queue) and time.monotonic() < stop:
        s = queue[i]
        try:
            changes = wayback_changes(s["url"], s["created"] - 30 * 86400)
        except urllib.error.HTTPError as e:
            errors[f"http {e.code}"] = errors.get(f"http {e.code}", 0) + 1
            if e.code in (429, 503):
                busy += 1
                if busy >= strikes:
                    break
                time.sleep(20 * busy)
                continue
            i += 1
            continue
        except Exception as e:
            key = f"{type(e).__name__}: {str(e)[:80]}"
            errors[key] = errors.get(key, 0) + 1
            i += 1
            continue
        busy = max(0, busy - 1)
        archive[s["id"]] = {"t": today, "last": max(changes) if changes else None, "v": len(changes)}
        done += 1
        i += 1
        time.sleep(pause)
    print(f"Internet Archive: looked up {done} of {len(queue)} queued sites"
          + (f"; errors {dict(sorted(errors.items(), key=lambda kv: -kv[1])[:4])}" if errors else ""))
    return archive


def check_all(sites, workers=48):
    def run(s):
        try:
            status, info = probe(s["url"])
        except Exception as e:
            status, info = DOWN, {"reason": _reason(e)}
        info["at"] = int(time.time())
        return status, info

    results = {}
    with ThreadPoolExecutor(workers) as ex:
        futures = {ex.submit(run, s): s["id"] for s in sites}
        wait(futures, timeout=PROBE_DEADLINE * 20)
        for f, sid in futures.items():
            results[sid] = f.result() if f.done() else (DOWN, {"reason": "timeout", "at": int(time.time())})
    # Give anything that looked down a second chance, so a brief blip isn't logged as an outage.
    retry = [s for s in sites if results[s["id"]][0] == DOWN]
    if retry:
        time.sleep(20)
        with ThreadPoolExecutor(workers) as ex:
            futures = {ex.submit(run, s): s["id"] for s in retry}
            wait(futures, timeout=PROBE_DEADLINE * 10)
            for f, sid in futures.items():
                if f.done() and f.result()[0] != DOWN:
                    results[sid] = f.result()
    return results


# --- Output -------------------------------------------------------------------------

def update_tracker(tracked, results, today, previous, started=None, archive=None):
    days = list(previous.get("days") or [])
    old = {s["id"]: s for s in previous.get("sites") or []}
    if not days or days[-1] != today:
        days.append(today)
    n = len(days)
    out = []
    for s in tracked:
        before = old.get(s["id"], {})
        history = (before.get("history") or "").ljust(n - 1, ".")[:n - 1]
        status, info = results.get(s["id"], (".", {}))
        history += status
        summary = info.pop("summary", "")
        fp, lm = info.pop("fp", None), info.pop("lm", None)
        last = {k: v for k, v in info.items() if v is not None}
        auto = category(s["title"], s["source"], summary) if summary else before.get("category_auto") or category(s["title"], s["source"])
        entry = {k: s[k] for k in ("id", "url", "domain", "title", "votes", "source", "sources", "post_url",
                                   "created", "cohort", "rank")}
        entry.update(category=s.get("category") or auto, category_auto=auto, hosting=hosting(s["domain"]),
                     history=history, last=last)
        entry["fp"] = fp or before.get("fp")
        changed = before.get("changed")
        if fp and before.get("fp") and fp != before["fp"]:
            changed = int(dt.datetime.fromisoformat(today).replace(tzinfo=dt.timezone.utc).timestamp())
        if changed:
            entry["changed"] = changed
        lm = lm or before.get("lm")
        if lm:
            entry["lm"] = lm
        wb = (archive or {}).get(s["id"], {}).get("last")
        known = [t for t in (changed, lm, wb) if t]
        if known:
            entry["updated"] = max(known)
        if not entry["fp"]:
            del entry["fp"]
        if status in (UP, WALLED):
            entry["last_up"] = today
        elif before.get("last_up"):
            entry["last_up"] = before["last_up"]
        out.append(entry)
    out.sort(key=lambda s: (s["cohort"], s["rank"]))
    return {"generated_at": now_utc().isoformat(timespec="seconds"), "checks_started": started,
            "start": START.date().isoformat(),
            "top_n": TOP_N, "sources": SOURCES, "days": days, "sites": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-collect", action="store_true", help="only check the sites already tracked")
    ap.add_argument("--archive-budget", type=int, default=ARCHIVE_BUDGET)
    ap.add_argument("--wayback-budget", type=int, default=WAYBACK_BUDGET)
    ap.add_argument("--wayback-minutes", type=float, default=WAYBACK_MINUTES)
    args = ap.parse_args()
    now = now_utc()
    posts, state = load(POSTS, {}), load(STATE, {})
    previous = load(OUT, {})
    if not args.no_collect:
        for step in (lambda: collect_deploy_list(posts), lambda: collect_hn(posts, state, now)):
            try:
                step()
            except Exception as e:
                print(f"warn: {e}", file=sys.stderr)
        try:
            api = collect_reddit_api(posts, state, now)
        except Exception as e:
            print(f"warn: Reddit API: {e}", file=sys.stderr)
            api = False
        if not api:
            collect_reddit(posts, state, now, args.archive_budget)
        save(POSTS, posts)
        save(STATE, state, pretty=True)
    known = {s["id"] for s in previous.get("sites") or []}
    tracked = [s for s in rank_sites(posts) if s["rank"] <= TOP_N or s["id"] in known]
    if not tracked:
        sys.exit("no posts collected; leaving the tracker unchanged")
    print(f"checking {len(tracked)} sites across {len({s['cohort'] for s in tracked})} launch months")
    started = now_utc().isoformat(timespec="seconds")
    results = check_all(tracked)
    archive = load(WAYBACK, {})
    try:
        update_wayback(tracked, archive, now.date().isoformat(), args.wayback_budget, args.wayback_minutes)
    except Exception as e:
        print(f"warn: archive lookups: {e}", file=sys.stderr)
    save(WAYBACK, archive)
    data = update_tracker(tracked, results, now.date().isoformat(), previous, started, archive)
    save(OUT, data)
    counts = {}
    for s in data["sites"]:
        counts[s["history"][-1]] = counts.get(s["history"][-1], 0) + 1
    print("today: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"archive history for {len(archive)} sites; change date known for {sum('updated' in s for s in data['sites'])}")


if __name__ == "__main__":
    main()
