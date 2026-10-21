import unittest
from datetime import timedelta, timezone

import analytics


class FakeStore:
    def __init__(self):
        self.saved = []
        self.pruned = None

    def save_stats_day(self, day, views, api_views, visitors, hours, dims):
        self.saved.append((day, views, api_views, visitors, hours, dims))

    def prune_stats(self, days):
        self.pruned = days


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_760_000_000.0]  # 2025-10-09 08:53 UTC
        self.store = FakeStore()
        self.stats = analytics.Stats(store=self.store, clock=lambda: self.now[0], tz=timezone(timedelta(hours=2)))
        self.headers = {"Referer": "https://www.google.com/search?q=x", "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8", "CF-IPCountry": "NL"}

    def test_counts_people_without_storing_who_they_are(self):
        ua = "Mozilla/5.0 (iPhone)"
        for _ in range(3):
            self.stats.count("page", "/", "203.0.113.9", ua, self.headers, own_host="planenoise.nl")
        self.stats.count("page", "/callsign.html?c=KLM41B", "203.0.113.10", "Mozilla/5.0", {"Referer": "https://planenoise.nl/"}, own_host="planenoise.nl")
        self.stats.count("api", "/recent_events?limit=200", "203.0.113.9", ua)
        self.stats.count("page", "/wp-login.php", "203.0.113.11", "python-requests", bot="script")
        self.assertEqual(self.stats.online(), 2)
        self.stats.flush()
        day, views, api_views, visitors, hours, dims = self.store.saved[0]
        self.assertEqual((views, api_views, visitors), (4, 1, 2))
        self.assertEqual(dims["page"], {"/": 3, "/callsign": 1})
        self.assertEqual(dims["referrer"], {"www.google.com": 3, "direct": 1})
        self.assertEqual(dims["language"]["nl"], 3)
        self.assertEqual(dims["device"], {"phone": 3, "desktop": 1})
        self.assertEqual(dims["country"]["NL"], 3)
        self.assertEqual(dims["api"], {"/recent_events": 1})
        self.assertEqual(dims["bot"], {"script": 1})
        self.assertEqual(len(hours), 1)
        hour_ts, (hviews, hvisitors) = next(iter(hours.items()))
        self.assertEqual(hour_ts % 3600, 0)
        self.assertEqual((hviews, hvisitors), (4, 2))
        blob = repr(self.store.saved)
        for secret in ("203.0.113", "iPhone", "KLM41B", "q=x"):
            self.assertNotIn(secret, blob)

    def test_a_visitor_is_new_every_day(self):
        self.stats.count("page", "/", "203.0.113.9", "ua")
        first = self.stats.visitor_hash(self.stats._day(self.stats._local(self.now[0])), "203.0.113.9", "ua")
        self.now[0] += 86400
        self.stats.count("page", "/", "203.0.113.9", "ua")
        second = self.stats.visitor_hash(self.stats._day(self.stats._local(self.now[0])), "203.0.113.9", "ua")
        self.assertNotEqual(first, second)
        self.stats.flush()
        self.assertEqual(len(self.store.saved), 2)
        self.assertEqual(len(self.stats._days), 1, "yesterday leaves memory after the flush")

    def test_keys_are_capped_and_rows_pruned(self):
        for i in range(analytics.MAX_KEYS + 40):
            self.stats.count("page", "/", "203.0.113.9", "ua", {"Referer": "https://site{0}.example/".format(i)})
        self.stats.flush()
        refs = self.store.saved[0][5]["referrer"]
        self.assertEqual(len(refs), analytics.MAX_KEYS + 1)
        self.assertEqual(refs["other"], 40)
        self.stats.retain()
        self.assertEqual(self.store.pruned, analytics.RETAIN_DAYS)

    def test_flush_only_when_due_and_dirty(self):
        self.stats.count("page", "/", "a", "ua")
        self.stats.flush_if_due()
        self.assertEqual(self.store.saved, [])
        self.now[0] += analytics.FLUSH_SECONDS
        self.stats.flush_if_due()
        self.assertEqual(len(self.store.saved), 1)
        self.now[0] += analytics.FLUSH_SECONDS
        self.stats.flush_if_due()
        self.assertEqual(len(self.store.saved), 1, "nothing new, nothing written")

    def test_campaigns_and_geolocation_without_an_edge(self):
        self.stats.count("page", "/", "203.0.113.9", "ua", {"Accept-Language": "nl"}, query="utm_source=forum&utm_campaign=oktober", country="nl", city="Uitgeest, NL")
        self.stats.count("page", "/", "203.0.113.9", "ua", {"CF-IPCountry": "DE"}, query="utm_source=newsletter", country="nl")
        self.stats.count("page", "/", "203.0.113.9", "ua", {}, query="ref=x")
        self.stats.flush()
        dims = self.store.saved[0][5]
        self.assertEqual(dims["campaign"], {"forum / oktober": 1, "newsletter": 1})
        self.assertEqual(dims["country"], {"NL": 1, "DE": 1, "unknown": 1}, "the edge header wins, the table fills in, else unknown")
        self.assertEqual(dims["city"], {"Uitgeest, NL": 1})

    def test_helpers(self):
        self.assertEqual(analytics.normalise_path("/api/callsigns/KLM41B/passes", "api"), "/api/callsigns/*")
        self.assertEqual(analytics.normalise_path("/anything/else", "page"), "/other")
        self.assertEqual(analytics.referrer_site("https://nos.nl/artikel/1", "planenoise.nl"), "nos.nl")
        self.assertEqual(analytics.referrer_site("https://www.planenoise.nl/x", "planenoise.nl"), "direct")
        self.assertEqual(analytics.language_tag("de-DE,de;q=0.9"), "de")
        self.assertEqual(analytics.language_tag(""), "unknown")
        self.assertEqual(analytics.country_code({"Cf-Ipcountry": "de"}), "DE")
        self.assertEqual(analytics.country_code({}), "unknown")

    def test_noise_against_visits(self):
        base = 1_760_000_000 - 1_760_000_000 % 3600
        hours, passes = [], {}
        for i in range(48):
            ts = base + i * 3600
            loud = i % 5 == 0   # a period that does not divide 24, so every hour of the day sees both kinds
            hours.append((ts, 20 if loud else 10, 5))
            passes[ts] = (6, 2 if loud else 0)
        out = analytics.noise_against_visits(hours, passes)
        self.assertEqual(out["ratio"], 2.0)
        self.assertEqual(out["loud_hours"], 10)
        self.assertEqual(analytics.noise_against_visits([], {})["ratio"], None)


class StatsHookTests(unittest.TestCase):
    """Requests through the server land in the statistics, bots apart from people."""

    @classmethod
    def setUpClass(cls):
        import os
        import tempfile
        os.environ["PN_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "stats.db")
        import proxy_server
        import guard
        from database import PlaneNoiseDB
        cls.server = proxy_server
        # Another module may have imported the server first, with the repo's own
        # database; this class always counts into a fresh temporary one.
        cls._orig_db = proxy_server.db
        proxy_server.db = PlaneNoiseDB(os.environ["PN_DB_PATH"])
        proxy_server._polling_started = True
        proxy_server.app.config["TESTING"] = True
        proxy_server.scraper_guard = guard.Guard(store=None, resolve=lambda ip: False)
        proxy_server.visitor_stats = analytics.Stats(store=proxy_server.db)
        cls.client = proxy_server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.db = cls._orig_db

    def test_pages_and_json_are_counted_bots_separately(self):
        person = {"REMOTE_ADDR": "203.0.113.20", "HTTP_USER_AGENT": "Mozilla/5.0 (Macintosh)", "HTTP_ACCEPT_LANGUAGE": "nl", "HTTP_REFERER": "https://nos.nl/"}
        self.assertEqual(self.client.get("/", environ_base=person).status_code, 200)
        self.assertEqual(self.client.get("/", environ_base=person).status_code, 200)
        self.assertEqual(self.client.get("/visitor/settings", environ_base=person).status_code, 200)
        self.client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.21", "HTTP_USER_AGENT": "python-requests/2.32"})
        self.client.get("/admin/login", environ_base=person)
        self.server.visitor_stats.flush()
        days = self.server.db.get_stats_days(2)
        self.assertEqual(len(days), 1)
        self.assertEqual((days[0]["views"], days[0]["visitors"], days[0]["api_views"]), (2, 1, 1))
        dims = {k: {r["key"]: r["n"] for r in self.server.db.get_stats_dims(2, k)} for k in analytics.KINDS}
        self.assertEqual(dims["page"], {"/": 2})
        self.assertEqual(dims["referrer"], {"nos.nl": 2})
        self.assertEqual(dims["language"], {"nl": 2})
        self.assertEqual(dims["api"], {"/visitor/settings": 1})
        self.assertEqual(dims["bot"], {"script": 1})
        self.assertEqual(self.server.visitor_stats.online(), 1)
        summary = self.client.get("/api/admin/analytics/summary")
        self.assertEqual(summary.status_code, 401, "the summary is for admins")


if __name__ == "__main__":
    unittest.main()
