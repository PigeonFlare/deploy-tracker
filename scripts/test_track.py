import http.server
import threading
import email.utils
import unittest
from unittest import mock

import track


class Handler(http.server.BaseHTTPRequestHandler):
    routes = {
        "/ok": (200, {}, b"<html><title>Puzzle</title><meta name=description content='a daily puzzle game'></html>"),
        "/parked": (200, {}, b"<html>This domain is for sale! Make an offer on this domain.</html>"),
        "/gone": (404, {}, b"The deployment could not be found. DEPLOYMENT_NOT_FOUND"),
        "/walled": (403, {}, b"denied"),
        "/challenge": (503, {"cf-mitigated": "challenge"}, b""),
        "/broken": (502, {}, b"bad gateway"),
        "/hop": (301, {"Location": "/ok"}, b""),
        "/lander-hop": (302, {"Location": "/lander"}, b""),
        "/lander": (200, {}, b"<html></html>"),
    }

    def do_GET(self):
        code, headers, body = self.routes.get(self.path, (404, {}, b""))
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://localhost:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def status(self, path):
        with mock.patch.object(track, "public_host", return_value=True):
            return track.probe(self.base + path)

    def test_statuses(self):
        self.assertEqual(self.status("/ok")[0], track.UP)
        self.assertIn("puzzle game", self.status("/ok")[1]["summary"])
        self.assertEqual(self.status("/hop")[0], track.UP)
        self.assertEqual(self.status("/parked")[0], track.PARKED)
        self.assertEqual(self.status("/lander-hop")[0], track.PARKED)
        self.assertEqual(self.status("/gone")[0], track.DOWN)
        self.assertEqual(self.status("/gone")[1]["reason"], "removed")
        self.assertEqual(self.status("/walled")[0], track.WALLED)
        self.assertEqual(self.status("/challenge")[0], track.WALLED)
        self.assertEqual(self.status("/broken")[1]["reason"], "http 5xx")

    def test_dns_failure(self):
        with mock.patch.object(track, "public_host", return_value=None):
            self.assertEqual(track.probe("https://nope.invalid/"), (track.DOWN, {"reason": "dns"}))


def post(url, votes, created, source="Show HN"):
    return {"url": url, "title": "t", "votes": votes, "source": source, "created": created}


class RankTest(unittest.TestCase):
    def test_dedupes_and_ranks_per_month(self):
        oct1, oct5, nov2 = 1759320000, 1759665600, 1762084800
        posts = {
            "a": post("https://www.a.com/", 50, oct5),
            "a2": post("https://a.com/index.html", 300, nov2, "r/SideProject"),
            "b": post("https://b.dev/", 120, oct1),
            "c": post("https://c.io/", 5, oct1),
        }
        sites = {s["id"]: s for s in track.rank_sites(posts)}
        self.assertEqual(set(sites), {"a.com", "b.dev"})
        self.assertEqual(sites["a.com"]["cohort"], "2025-10")
        self.assertEqual(sites["a.com"]["votes"], 300)
        self.assertEqual(sites["a.com"]["sources"], ["Show HN", "r/SideProject"])
        self.assertEqual((sites["b.dev"]["rank"], sites["a.com"]["rank"]), (2, 1))

    def test_add_post_keeps_highest_votes(self):
        posts = {}
        track.add_post(posts, "p", "https://x.com/", "t", 40, "r/SaaS", 1760000000)
        track.add_post(posts, "p", "https://x.com/", "t", 12, "r/SaaS", 1760000000)
        self.assertEqual(posts["p"]["votes"], 40)
        self.assertFalse(track.add_post(posts, "q", "https://y.com/", "t", 40, "r/SaaS", 1600000000))


class RedditTest(unittest.TestCase):
    def test_subreddits_take_turns_and_resume(self):
        calls, slowed = [], []

        def fake_get(params, left, path="search"):
            if path == "ids":
                return [{"id": i, "selftext": f"try it at https://{i}.app"} for i in params["ids"].split(",")]
            if left[0] <= 0:
                raise track.ArchivePaused("budget")
            if len(calls) == 3 and not slowed:
                slowed.append(True)
                raise track.ArchivePaused("HTTP 422")
            left[0] -= 1
            calls.append(params["subreddit"])
            t = params["after"] + 60
            return [{"id": f"x{t}", "title": "I made a site", "url": f"https://s{t}.com/", "score": 50,
                     "created_utc": t},
                    {"id": f"y{t}", "title": "I made this", "url": f"https://www.reddit.com/r/x/comments/y{t}/",
                     "score": 50, "created_utc": t},
                    {"id": f"z{t}", "title": "meh", "url": f"https://z{t}.com/", "score": 2, "created_utc": t},
                    ] if t < 1759400000 else []

        state, posts = {}, {}
        now = track.dt.datetime(2026, 10, 9, tzinfo=track.dt.timezone.utc)
        with mock.patch.object(track, "archive_get", side_effect=fake_get), mock.patch.object(track.time, "sleep"):
            track.collect_reddit(posts, state, now, budget=10)
        self.assertEqual(calls[:5], track.SUBREDDITS)
        self.assertEqual(len(calls), 10)
        self.assertEqual(len(state["reddit_cursors"]), 5)
        self.assertEqual(len(posts), 20)
        self.assertIn("https://y1759276860.app/", {p["url"] for p in posts.values()})


class RedditApiTest(unittest.TestCase):
    def test_reads_a_year_of_top_posts_then_a_month(self):
        urls = []

        def fake_fetch(url, timeout=30, headers=None, data=None):
            urls.append(url)
            child = {"data": {"title": "I built a tiny game", "url": "https://tiny.game/", "score": 900,
                              "permalink": "/r/x/comments/abc/", "created_utc": 1770000000}}
            return track.json.dumps({"data": {"children": [child], "after": None}}).encode()

        state, posts = {}, {}
        now = track.dt.datetime(2026, 10, 9, tzinfo=track.dt.timezone.utc)
        with mock.patch.object(track, "reddit_token", return_value="t"), \
                mock.patch.object(track, "fetch", side_effect=fake_fetch), mock.patch.object(track.time, "sleep"):
            self.assertTrue(track.collect_reddit_api(posts, state, now))
            self.assertTrue(all("t=year" in u for u in urls))
            urls.clear()
            track.collect_reddit_api(posts, state, now + track.dt.timedelta(days=1))
            self.assertTrue(all("t=month" in u for u in urls))
        self.assertEqual(posts["https://www.reddit.com/r/x/comments/abc/"]["votes"], 900)


class TrackerTest(unittest.TestCase):
    def test_history_grows_one_char_per_day(self):
        site = {"id": "a.com", "url": "https://a.com/", "domain": "a.vercel.app", "title": "my game", "votes": 20,
                "source": "Show HN", "sources": ["Show HN"], "post_url": "p", "created": 1760000000,
                "cohort": "2025-10", "rank": 1}
        day1 = track.update_tracker([site], {"a.com": ("U", {"code": 200})}, "2026-10-09", {})
        new = dict(site, id="b.com")
        day2 = track.update_tracker([site, new], {"a.com": ("D", {"reason": "dns"}), "b.com": ("U", {})},
                                    "2026-10-10", day1)
        by_id = {s["id"]: s for s in day2["sites"]}
        self.assertEqual(day2["days"], ["2026-10-09", "2026-10-10"])
        self.assertEqual(by_id["a.com"]["history"], "UD")
        self.assertEqual(by_id["a.com"]["last_up"], "2026-10-09")
        self.assertEqual(by_id["b.com"]["history"], ".U")
        self.assertEqual(by_id["a.com"]["hosting"], "vercel.app")
        self.assertEqual(by_id["a.com"]["category"], "games")


class ChangeTest(unittest.TestCase):
    def test_fingerprint_ignores_digits_and_scripts_but_not_bundles(self):
        a = '<html><script>var t=1</script><script src="/app.abc.js"></script><p>Hello 12:01</p></html>'
        b = '<html><script>var t=2</script><script src="/app.abc.js"></script><p>Hello 12:02</p></html>'
        c = a.replace("app.abc.js", "app.def.js")
        self.assertEqual(track.fingerprint(a), track.fingerprint(b))
        self.assertNotEqual(track.fingerprint(a), track.fingerprint(c))
        self.assertNotEqual(track.fingerprint(a), track.fingerprint(a.replace("Hello", "Howdy")))

    def test_last_modified_skips_the_current_time(self):
        now = 1791500000
        self.assertIsNone(track.last_modified(email.utils.formatdate(now - 60, usegmt=True), now))
        self.assertEqual(track.last_modified(email.utils.formatdate(now - 86400 * 9, usegmt=True), now), now - 86400 * 9)
        self.assertIsNone(track.last_modified("garbage", now))

    def test_updated_takes_the_latest_signal(self):
        site = {"id": "a.com", "url": "https://a.com/", "domain": "a.com", "title": "tool", "votes": 20,
                "source": "Show HN", "sources": ["Show HN"], "post_url": "p", "created": 1760000000,
                "cohort": "2025-10", "rank": 1}
        day1 = track.update_tracker([site], {"a.com": ("U", {"fp": "x", "lm": 1770000000})}, "2026-10-09", {},
                                    archive={"a.com": {"last": 1780000000}})
        self.assertEqual(day1["sites"][0]["updated"], 1780000000)
        self.assertNotIn("changed", day1["sites"][0])
        day2 = track.update_tracker([site], {"a.com": ("U", {"fp": "y"})}, "2026-10-10", day1,
                                    archive={"a.com": {"last": 1780000000}})
        s = day2["sites"][0]
        self.assertEqual(s["fp"], "y")
        self.assertEqual(s["changed"], 1791590400)
        self.assertEqual(s["updated"], 1791590400)
        self.assertEqual(s["lm"], 1770000000)

    def test_wayback_stops_when_throttled(self):
        sites = [{"id": f"s{i}.com", "url": f"https://s{i}.com/", "created": 1760000000} for i in range(5)]
        calls = []

        def fake(url, since):
            calls.append(url)
            if url.startswith("https://s0"):
                return [1770000000, 1775000000]
            raise track.urllib.error.HTTPError(url, 429, "slow down", {}, None)

        archive = {}
        with mock.patch.object(track, "wayback_changes", side_effect=fake):
            track.update_wayback(sites, archive, "2026-10-09", workers=1)
        self.assertEqual(archive["s0.com"], {"t": "2026-10-09", "last": 1775000000, "v": 2})
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
