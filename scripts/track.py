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
import datetime as dt
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

UA = "deploy-tracker/1.0 (+https://github.com/PigeonFlare/deploy-tracker)"
START = dt.datetime(2025, 10, 1, tzinfo=dt.timezone.utc)
TOP_N = 100
MIN_VOTES = 10
DEPLOY_LIST_FEED = "https://raw.githubusercontent.com/PigeonFlare/deploy-list/main/site/data/sites.json"
SUBREDDITS = ["SideProject", "InternetIsBeautiful", "IMadeThis", "ClaudeAI", "SaaS"]
MAKER_ONLY = {"ClaudeAI", "SaaS"}
SOURCES = [{"name": "Show HN", "url": "https://news.ycombinator.com/show"}] + [
    {"name": f"r/{s}", "url": f"https://www.reddit.com/r/{s}/"} for s in SUBREDDITS]

ARCHIVE_API = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCHIVE_BUDGET = 60
ARCHIVE_PAUSE = 2
ARCHIVE_SETTLE = 36 * 3600  # the archive re-reads a post's score about 36 hours after it's posted
ARCHIVE_FIELDS = "id,title,url,selftext,score,created_utc,over_18"

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
PROBE_DEADLINE = 30
PROBE_HOPS = 6


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
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


def archive_get(params, budget):
    if budget[0] <= 0:
        raise ArchivePaused("request budget spent")
    budget[0] -= 1
    time.sleep(ARCHIVE_PAUSE)
    try:
        data = json.loads(fetch(f"{ARCHIVE_API}?{urllib.parse.urlencode(params)}", timeout=60))
    except urllib.error.HTTPError as e:
        if e.code in (422, 429, 503):
            raise ArchivePaused(f"HTTP {e.code}")
        raise
    if data.get("error"):
        raise ArchivePaused(str(data["error"]))
    return data.get("data") or []


def collect_reddit(posts, state, now, budget=ARCHIVE_BUDGET):
    """Walk each subreddit forward from START through the archive, oldest first, stopping
    ARCHIVE_SETTLE short of now so the scores read are settled. The cursor is saved, so a
    run that spends its budget picks up where it left off."""
    cursors = state.setdefault("reddit_cursors", {})
    left = [budget]
    new = 0
    limit = state.get("archive_limit", "auto")
    try:
        for sub in SUBREDDITS:
            stop = int(now.timestamp() - ARCHIVE_SETTLE)
            while cursors.get(sub, int(START.timestamp())) < stop:
                after = cursors.get(sub, int(START.timestamp()))
                params = {"subreddit": sub, "after": after, "before": stop, "sort": "asc",
                          "limit": limit, "fields": ARCHIVE_FIELDS}
                try:
                    batch = archive_get(params, left)
                except urllib.error.HTTPError as e:
                    if e.code == 400 and limit != 100:
                        limit = state["archive_limit"] = 100
                        continue
                    raise
                for p in batch:
                    title = p.get("title") or ""
                    if p.get("over_18") or (sub in MAKER_ONLY and not MAKER_TITLE.search(title)):
                        continue
                    if not isinstance(p.get("score"), int) or p["score"] < MIN_VOTES:
                        continue
                    url = pick_site(title, p.get("url"), URL_RE.findall(p.get("selftext") or ""))
                    new += add_post(posts, f"https://www.reddit.com/r/{sub}/comments/{p['id']}/", url, title,
                                    p["score"], f"r/{sub}", p.get("created_utc"))
                if not batch:
                    cursors[sub] = stop
                    break
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
            return UP, info
    return DOWN, {"reason": "redirect loop"}


def check_all(sites, workers=48):
    def run(s):
        try:
            return probe(s["url"])
        except Exception as e:
            return DOWN, {"reason": _reason(e)}

    results = {}
    with ThreadPoolExecutor(workers) as ex:
        futures = {ex.submit(run, s): s["id"] for s in sites}
        wait(futures, timeout=PROBE_DEADLINE * 20)
        for f, sid in futures.items():
            results[sid] = f.result() if f.done() else (DOWN, {"reason": "timeout"})
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

def update_tracker(tracked, results, today, previous):
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
        last = {k: v for k, v in info.items() if v is not None}
        auto = category(s["title"], s["source"], summary) if summary else before.get("category_auto") or category(s["title"], s["source"])
        entry = {k: s[k] for k in ("id", "url", "domain", "title", "votes", "source", "sources", "post_url",
                                   "created", "cohort", "rank")}
        entry.update(category=s.get("category") or auto, category_auto=auto, hosting=hosting(s["domain"]),
                     history=history, last=last)
        if status in (UP, WALLED):
            entry["last_up"] = today
        elif before.get("last_up"):
            entry["last_up"] = before["last_up"]
        out.append(entry)
    out.sort(key=lambda s: (s["cohort"], s["rank"]))
    return {"generated_at": now_utc().isoformat(timespec="seconds"), "start": START.date().isoformat(),
            "top_n": TOP_N, "sources": SOURCES, "days": days, "sites": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-collect", action="store_true", help="only check the sites already tracked")
    ap.add_argument("--archive-budget", type=int, default=ARCHIVE_BUDGET)
    args = ap.parse_args()
    now = now_utc()
    posts, state = load(POSTS, {}), load(STATE, {})
    previous = load(OUT, {})
    if not args.no_collect:
        for step in (lambda: collect_deploy_list(posts), lambda: collect_hn(posts, state, now),
                     lambda: collect_reddit(posts, state, now, args.archive_budget)):
            try:
                step()
            except Exception as e:
                print(f"warn: {e}", file=sys.stderr)
        save(POSTS, posts)
        save(STATE, state, pretty=True)
    known = {s["id"] for s in previous.get("sites") or []}
    tracked = [s for s in rank_sites(posts) if s["rank"] <= TOP_N or s["id"] in known]
    if not tracked:
        sys.exit("no posts collected; leaving the tracker unchanged")
    print(f"checking {len(tracked)} sites across {len({s['cohort'] for s in tracked})} launch months")
    results = check_all(tracked)
    data = update_tracker(tracked, results, now.date().isoformat(), previous)
    save(OUT, data)
    counts = {}
    for s in data["sites"]:
        counts[s["history"][-1]] = counts.get(s["history"][-1], 0) + 1
    print("today: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
