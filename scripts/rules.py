"""Post filters and categories shared with deploy-list (github.com/PigeonFlare/deploy-list,
scripts/scrape.py), copied so both projects judge "a standalone website" the same way."""

import html
import ipaddress
import re
import socket
import urllib.parse

MAKER_TITLE = re.compile(
    r"\b(i|we|i've|we've|i'm|we're|my|our)\b|^(made|built|created|launched|introducing my)\b|"
    r"\b(made|built|build|building|created|launched|shipped|coded|vibe-?coded|remade|recreated)\b", re.I)

# Hosts that are never "a standalone website" for our purposes.
BLOCKED_HOSTS = {
    "github.com", "gist.github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "sr.ht",
    "apps.apple.com", "itunes.apple.com", "play.google.com", "chromewebstore.google.com",
    "chrome.google.com", "addons.mozilla.org", "microsoftedge.microsoft.com", "store.steampowered.com",
    "itch.io", "producthunt.com", "npmjs.com", "pypi.org", "crates.io", "huggingface.co",
    "marketplace.visualstudio.com", "kickstarter.com", "indiegogo.com",
    "medium.com", "substack.com", "dev.to", "hashnode.dev", "wordpress.com", "blogspot.com",
    "notion.site", "notion.so", "docs.google.com", "drive.google.com", "forms.gle",
    "youtube.com", "youtu.be", "vimeo.com", "loom.com", "twitter.com", "x.com", "linkedin.com",
    "facebook.com", "instagram.com", "tiktok.com", "threads.net", "bsky.app", "mastodon.social",
    "reddit.com", "redd.it", "redditstatic.com", "redditmedia.com", "reddit.app.link", "onelink.me", "app.link", "imgur.com", "i.imgur.com", "news.ycombinator.com", "discord.gg",
    "discord.com", "t.me", "arxiv.org", "wikipedia.org", "bit.ly", "tinyurl.com", "linktr.ee",
    "deploylist.com", "pigeonflare.github.io",
    # AI vendors' own pages (announcements, docs, chat links)
    "anthropic.com", "claude.ai", "claude.com", "openai.com", "chatgpt.com", "gemini.google.com",
    # News and publishing
    "businessinsider.com", "theverge.com", "techcrunch.com", "wired.com", "arstechnica.com", "reuters.com",
    "bloomberg.com", "cnbc.com", "cnn.com", "bbc.com", "bbc.co.uk", "nytimes.com", "theguardian.com",
    "forbes.com", "axios.com", "wsj.com", "ft.com", "404media.co", "tomshardware.com", "xda-developers.com",
    "gamesradar.com", "pcgamer.com", "theregister.com", "zdnet.com", "engadget.com", "venturebeat.com",
    "leaddev.com", "infoq.com", "martinfowler.com",
}
BLOG_PATH = re.compile(r"/(blog|posts?|articles?|news|p|story|stories|writing|essays?|\d{4}/\d{2})(/|$)", re.I)
BLOG_TITLE = re.compile(r"^(how|why|what) i\b|\bi wrote\b|\bwrite-?up\b|\bpost-?mortem\b|\blessons learned\b|\bblog\b|"
                        r"^(why|how to|hot take|opinion|explaining|introducing|announcing)\b", re.I)
# Words that mean the post is about a game. Only the post title is held to this list:
# a site's own description mentions games in passing far too often ("mini games",
# "references to memes, games, films", "a cursor, a clicker and a slide remote").
GAME_WORDS = re.compile(
    r"\b(game|gameplay|playable|puzzle game|wordle|chess|arcade|trivia|sudoku|crossword|"
    r"roguelike|roguelite|platformer|clicker game|idle game|quiz game|geoguessr|tetris|minesweeper|solitaire|rpg|mmo|io game|"
    r"shooter|stickman|pok[eé]mon|tower defense|pinball|speedrun|match-3|flight simulator)\b", re.I)
# In a site's description, only phrases that say the site itself is a game.
GAME_PAGE = re.compile(
    r"\b(?:(?:a|an|free|online|browser|web|multiplayer|puzzle|word|card|board|idle|casual|indie|daily|"
    r"retro|pixel|arcade|strategy|racing|platform|survival|physics|rhythm|trivia|2d|3d)[- ]game|"
    r"play (?:it )?(?:now|free|online|for free|in your browser)|roguelike|roguelite|platformer|sudoku|crossword|"
    r"wordle|tetris|minesweeper|solitaire|tower defense|pinball|match-3|geoguessr)\b", re.I)
APP_WORDS = re.compile(
    r"\b(app|apps|tool|tools|editor|generator|tracker|converter|platform|dashboard|ai|saas|api|manager|"
    r"builder|calculator|planner|extension|assistant|analytics|search|engine|notes?|budget|finance|"
    r"crm|chat|convert|compress|pdf|resume|invoice|scheduler|calendar|monitor|ide|cli|database|workflow|"
    r"agents?|harness|workspace|sandbox|email|course|learn|translate|password|tasks?|todo|productivity)\b", re.I)
NSFW = re.compile(r"\bnsfw\b|\bporn|\bonlyfans\b|\bxxx\b", re.I)
URL_RE = re.compile(r"https?://[^\s)\]>\"'|]+")


def host_of(url):
    h = (urllib.parse.urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def blocked(host):
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


def is_ip(host):
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def site_url(url, title):
    """Return a cleaned URL if this link is a standalone website, else None."""
    url = html.unescape(url).rstrip(".,;:!?")
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return None
    host = host_of(url)
    if parts.scheme not in ("http", "https") or not host or "." not in host or is_ip(host) or blocked(host):
        return None
    try:
        port = parts.port  # raises ValueError for malformed or out-of-range ports
    except ValueError:
        return None
    if port and port not in (80, 443, 8080, 8443):
        return None
    if host.startswith(("blog.", "engineering.")) or host.endswith(".engineering") or BLOG_PATH.search(parts.path) or BLOG_TITLE.search(title or ""):
        return None
    if re.search(r"\.(pdf|png|jpe?g|gif|mp4|zip)$", parts.path, re.I):
        return None
    netloc = (parts.hostname or "") + (f":{port}" if port else "")
    if not netloc:
        return None
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def page_summary(body):
    """The words a page uses to describe itself: its title, description and keywords."""
    text = body.decode("utf-8", "replace")
    parts = re.findall(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)[:1]
    for m in re.finditer(r"<meta\b[^>]*>", text, re.I):
        tag = m.group(0)
        if re.search(r"""(name|property)\s*=\s*["']?(description|keywords|og:title|og:description|twitter:description)["'\s>]""", tag, re.I):
            c = re.search(r"""content\s*=\s*(["'])(.*?)\1""", tag, re.I | re.S)
            if c:
                parts.append(c.group(2))
    return html.unescape(" ".join(p.strip() for p in parts))[:2000]


def category(title, source, summary=""):
    """The post title decides first; the site's own description only fills in when the
    title doesn't say what the project is."""
    if source == "r/WebGames" or GAME_WORDS.search(title):
        return "games"
    if APP_WORDS.search(title):
        return "apps"
    if GAME_PAGE.search(summary):
        return "games"
    if APP_WORDS.search(summary):
        return "apps"
    return "other"


def pick_site(title, direct, body_links=()):
    """The post's site: its link if it has one, else the single site its text links to."""
    url = site_url(direct or "", title)
    if url:
        return url
    links = {site_url(u, title) for u in body_links} - {None}
    if len({host_of(u) for u in links}) != 1:
        return None
    return sorted(links, key=len)[0]


def public_host(host):
    """True when every address the host resolves to is on the public internet;
    None when the name doesn't resolve at all."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


