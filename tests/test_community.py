import json
import math
import os
import time
import tempfile
import unittest

os.environ["COMMUNITY_SKIP_REMOTE"] = "1"

import community


def _haversine_m(lat1, lon1, lat2, lon2):
    radius = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


class CommunityRecordTests(unittest.TestCase):
    def setUp(self):
        self._real_db = community.db
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        os.environ["COMMUNITY_DATA_PATH"] = self.tmp.name
        community._invalidate_cache()
        community._cache["last_good"] = None
        community._cache["failed_until"] = 0
        with open(self.tmp.name, "w", encoding="utf-8") as handle:
            json.dump({"monitors": [], "last_updated": None}, handle)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _settings(self, **extra):
        settings = {
            "sensor_lat": "52.5154",
            "sensor_lon": "4.7206",
            "sensor_id": "69616",
            "site_place_name": "Uitgeest",
            "site_country": "nl",
            "community_public_url": "https://planenoise.nl",
        }
        settings.update(extra)
        return settings

    def test_record_hides_exact_location_and_sensor_id(self):
        record, reason = community.build_record(self._settings())
        self.assertIsNone(reason)
        encoded = json.dumps(record)
        self.assertNotIn("69616", encoded)
        self.assertNotIn("sensor_id", encoded)
        distance = _haversine_m(52.5154, 4.7206, record["latitude"], record["longitude"])
        self.assertGreaterEqual(distance, 60)
        self.assertLessEqual(distance, 200)
        again, _ = community.build_record(self._settings())
        self.assertEqual((record["latitude"], record["longitude"]), (again["latitude"], again["longitude"]))
        self.assertEqual(record["url"], "https://planenoise.nl")
        self.assertFalse(record["local_only"])
        self.assertEqual(record["country_code"], "NL")

    def test_lan_address_is_not_linked(self):
        record, reason = community.build_record(self._settings(community_public_url="http://192.168.1.20:8787"))
        self.assertIsNone(reason)
        self.assertIsNone(record["url"])
        self.assertTrue(record["local_only"])

    def test_pass_payload_carries_no_position_and_derives_the_slant(self):
        row = {"id": 7, "timestamp": 1760955600.0, "icao24": "4CAD96", "callsign": "KLM41B", "bucket_5m": 5869852,
               "manufacturer": "Embraer", "model": "ERJ-170-200 (long wing)", "operator": "KLM", "route_kind": "departure",
               "route_airport": "EHAM", "route_runway": "36L", "route_label": "EHAM 36L departure", "spl_db": 56.1, "spl_max_db": 74.4,
               "distance_km": 1.32, "altitude_m": 1836, "latitude": 52.5, "longitude": 4.7, "typecode": "E175", "peak_slant_km": None}
        p = community.pass_payload(row)
        self.assertEqual(p["uid"], "4cad96:5869852")
        self.assertEqual(p["type_family"], "E175")
        self.assertEqual(p["operator_code"], "KLM")
        self.assertEqual(p["slant_m"], 2261)
        self.assertNotIn("latitude", p)
        self.assertNotIn("longitude", p)
        self.assertNotIn("callsign", p)
        row["peak_slant_km"] = 1.8
        self.assertEqual(community.pass_payload(row)["slant_m"], 1800)

    def test_share_posts_the_record_to_the_aggregator_and_never_github(self):
        calls = []

        class Response:
            ok = True
            status_code = 200

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers))
            return Response()

        original = community.requests.post
        community.requests.post = fake_post
        self.addCleanup(lambda: setattr(community.requests, "post", original))
        os.environ.pop("COMMUNITY_SKIP_REMOTE", None)
        self.addCleanup(lambda: os.environ.__setitem__("COMMUNITY_SKIP_REMOTE", "1"))

        keys = {}
        community.db = type("FakeDB", (), {
            "get_admin_setting": lambda self, k, d=None: keys.get(k, d),
            "set_admin_setting": lambda self, k, v: keys.__setitem__(k, v),
        })()
        self.addCleanup(lambda: setattr(community, "db", self._real_db))

        record, _ = community.build_record(self._settings(site_timezone="Europe/Amsterdam"))
        self.assertEqual(record["timezone"], "Europe/Amsterdam")
        result = community.post_remote(community.DEFAULT_AGGREGATOR, {"monitor": record})

        self.assertEqual(result, "sent")
        url, body, headers = calls[0]
        self.assertEqual(url, "https://data.aircraftnoise.eu/api/v1/stations")
        self.assertEqual(body["monitor"]["id"], record["id"])
        self.assertEqual(len(headers["X-Station-Key"]), 48)
        self.assertEqual(keys["api_secret.community_station_key"], headers["X-Station-Key"])
        self.assertEqual(community.station_key(), headers["X-Station-Key"], "the key is made once")
        self.assertNotIn("69616", json.dumps(body))
        self.assertFalse(hasattr(community, "push_github"))
        self.assertFalse(hasattr(community, "ingest_body"))
        self.assertEqual(community.passes_url({}, record["id"]), "https://data.aircraftnoise.eu/api/v1/stations/" + record["id"] + "/passes")

    def test_sensor_position_comes_from_the_saved_sensor_keys(self):
        import os, tempfile
        os.environ["PN_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "pos.db")
        import proxy_server
        from database import PlaneNoiseDB
        orig = proxy_server.db
        proxy_server.db = PlaneNoiseDB(os.environ["PN_DB_PATH"])
        self.addCleanup(lambda: setattr(proxy_server, "db", orig))
        self.assertEqual(proxy_server.sensor_position(), (0.0, 0.0), "nothing saved: not set")
        proxy_server.db.set_admin_setting("sensor_lat", "51.9")
        proxy_server.db.set_admin_setting("sensor_lon", "4.4")
        self.assertEqual(proxy_server.sensor_position(), (51.9, 4.4), "the keys the admin page saves")
        proxy_server.db.set_admin_setting("latitude", "52.40")
        proxy_server.db.set_admin_setting("longitude", "4.90")
        self.assertEqual(proxy_server.sensor_position(), (52.40, 4.90), "a saved zone centre wins over the microphone")
        proxy_server.db.set_admin_setting("latitude", "")
        proxy_server.db.set_admin_setting("longitude", "")
        lo, left, hi, right = proxy_server._sensor_bbox(8.0)
        self.assertTrue(lo < 51.9 < hi and left < 4.4 < right, "the feed box is around the saved sensor")

    def test_admin_cookie_is_secure_only_on_https_visits(self):
        import os, tempfile
        os.environ["PN_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "cookie.db")
        import proxy_server
        from database import PlaneNoiseDB
        orig = proxy_server.db
        proxy_server.db = PlaneNoiseDB(os.environ["PN_DB_PATH"])
        self.addCleanup(lambda: setattr(proxy_server, "db", orig))
        proxy_server._polling_started = True
        proxy_server.app.config["TESTING"] = True
        c = proxy_server.app.test_client()
        with proxy_server.app.test_request_context("/", base_url="http://nas:8787"):
            self.assertFalse(proxy_server.app.session_interface.get_cookie_secure(proxy_server.app), "plain LAN http: no Secure flag")
        with proxy_server.app.test_request_context("/", base_url="https://station.example"):
            self.assertTrue(proxy_server.app.session_interface.get_cookie_secure(proxy_server.app))
        with proxy_server.app.test_request_context("/", base_url="http://station.example", headers={"X-Forwarded-Proto": "https"}):
            self.assertTrue(proxy_server.app.session_interface.get_cookie_secure(proxy_server.app), "TLS at the tunnel or proxy counts")

    def test_public_map_zoom_is_a_visitor_setting(self):
        import os, tempfile
        os.environ["PN_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "zoom.db")
        import proxy_server
        from database import PlaneNoiseDB
        orig = proxy_server.db
        proxy_server.db = PlaneNoiseDB(os.environ["PN_DB_PATH"])
        self.addCleanup(lambda: setattr(proxy_server, "db", orig))
        proxy_server._polling_started = True
        proxy_server.app.config["TESTING"] = True
        c = proxy_server.app.test_client()
        self.assertIsNone(c.get("/visitor/settings").get_json()["map_zoom"], "automatic by default")
        proxy_server.db.set_admin_setting("public_map_zoom", "9")
        self.assertEqual(c.get("/visitor/settings").get_json()["map_zoom"], 9)
        proxy_server.db.set_admin_setting("public_map_zoom", "99")
        self.assertIsNone(c.get("/visitor/settings").get_json()["map_zoom"], "out of range falls back to automatic")

    def test_public_pages_carry_the_configured_station_address(self):
        import os, tempfile
        os.environ["PN_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "url.db")
        import proxy_server
        from database import PlaneNoiseDB
        orig = proxy_server.db
        proxy_server.db = PlaneNoiseDB(os.environ["PN_DB_PATH"])
        self.addCleanup(lambda: setattr(proxy_server, "db", orig))
        proxy_server._polling_started = True
        proxy_server.app.config["TESTING"] = True
        c = proxy_server.app.test_client()
        page = c.get("/").get_data(as_text=True)
        self.assertIn("This station is not set up yet", page, "a fresh install points the first visitor to the setup")
        self.assertNotIn("station.example", page)
        self.assertNotIn("planenoise.nl", page)
        self.assertIn('<link rel="canonical" href="http://localhost/" />', page)
        proxy_server.db.set_admin_setting("community_public_url", "https://www.uitgeest.example.org/")
        page = c.get("/").get_data(as_text=True)
        self.assertIn('<link rel="canonical" href="https://uitgeest.example.org/" />', page)
        self.assertIn('content="https://uitgeest.example.org/static/og-image.png"', page)
        self.assertIn("https://uitgeest.example.org/sitemap.xml", c.get("/robots.txt").get_data(as_text=True))
        self.assertIn("<loc>https://uitgeest.example.org/community</loc>", c.get("/sitemap.xml").get_data(as_text=True))
        self.assertEqual(c.get("/seo-content.html").status_code, 404)

    def test_station_id_is_fixed_once_made_and_survives_an_address_change(self):
        keys = {}
        community.db = type("FakeDB", (), {
            "get_admin_setting": lambda self, k, d=None: keys.get(k, d),
            "set_admin_setting": lambda self, k, v: keys.__setitem__(k, v),
        })()
        self.addCleanup(lambda: setattr(community, "db", self._real_db))
        settings = self._settings(); settings["community_public_url"] = "https://old.example.org"
        first = community.station_id(settings)
        self.assertEqual(first, community.monitor_id("https://old.example.org"))
        self.assertEqual(keys["community_station_id"], first)
        settings["community_public_url"] = "https://new.example.net"
        settings["community_station_id"] = first
        record, _ = community.build_record(settings)
        self.assertEqual(record["id"], first, "a new address must not make a new station")
        self.assertEqual(record["url"], "https://new.example.net")
        # An install that shared before this rule keeps the id the service knows it by.
        keys.clear()
        adopted = community.station_id({"community_last_id": "abcdef0123456789", "community_public_url": "https://x.example"})
        self.assertEqual(adopted, "abcdef0123456789")
        self.assertEqual(keys["community_station_id"], "abcdef0123456789")

    def test_history_backfill_walks_the_past_in_windows_and_can_resume(self):
        keys = {"community_enabled": "true", "community_last_id": "abc123"}
        now = time.time()
        first = now - 30 * 86400
        posted = []
        asked = []

        class FakeDB:
            def get_all_admin_settings(self): return dict(keys)
            def get_admin_setting(self, k, d=None): return keys.get(k, d)
            def set_admin_setting(self, k, v): keys[k] = v
            judged = []
            def first_pass_ts(self): return first
            def count_unflagged_passes(self): return 0 if self.judged else 5
            def recompute_zone_flags(self, test): self.judged.append(test); return 5
            def count_passes_for_sharing(self, a, b): return 3
            def get_passes_for_sharing(self, since, until=None):
                asked.append((since, until))
                return [{"icao24": "484a2b", "timestamp": since + 60, "bucket_5m": 1, "spl_max_db": 60, "spl_db": 55}] if since < now - 20 * 86400 else []
        community.db = FakeDB()
        self.addCleanup(lambda: setattr(community, "db", self._real_db))
        os.environ.pop("COMMUNITY_SKIP_REMOTE", None)
        self.addCleanup(lambda: os.environ.__setitem__("COMMUNITY_SKIP_REMOTE", "1"))
        original = (community.requests.post, community.BACKFILL_PAUSE_SECONDS)
        self.addCleanup(lambda: (setattr(community.requests, "post", original[0]), setattr(community, "BACKFILL_PAUSE_SECONDS", original[1])))
        community.BACKFILL_PAUSE_SECONDS = 0

        class Up:
            ok = True; status_code = 200; headers = {}
            def json(self): return {"stored": 1, "skipped": 0}
        community.requests.post = lambda url, json=None, headers=None, timeout=None: posted.append(json) or Up()
        orig_zone = community.zone_test
        community.zone_test = lambda: (lambda lat, lon, d: True)
        self.addCleanup(lambda: setattr(community, "zone_test", orig_zone))
        community._backfill_run()
        self.assertEqual(len(community.db.judged), 1, "old passes without a zone flag are judged first")
        self.assertEqual(keys["community_backfill_state"], "done")
        self.assertEqual(int(keys["community_backfill_sent"]), len(posted))
        self.assertEqual(int(keys["community_backfill_stored"]), len(posted), "what the service stored is counted from its answers")
        self.assertGreaterEqual(len(asked), 3, "windows of a week over 23 days of history")
        self.assertLess(asked[-1][1], now - 7 * 86400 + 5, "the last week belongs to the regular loop")
        self.assertEqual(asked[0][0], first)
        # A failure keeps the place reached; the next run continues from there.
        class Down:
            ok = False; status_code = 502; headers = {}
        posted.clear(); asked.clear()
        keys["community_backfill_state"] = "stopped"; keys["community_backfill_reached_ts"] = str(first + 7 * 86400)
        community.requests.post = lambda url, json=None, headers=None, timeout=None: posted.append(json) or Down()
        community._backfill_run()
        self.assertEqual(keys["community_backfill_state"], "failed")
        self.assertEqual(asked[0][0], first + 7 * 86400)
        self.assertEqual(keys["community_backfill_reached_ts"], str(first + 7 * 86400))

    def test_www_and_bare_domain_are_the_same_station(self):
        self.assertEqual(community.monitor_id("https://www.example.org/"), community.monitor_id("https://example.org"))
        self.assertEqual(community.canonical_url("HTTPS://WWW.Example.org/"), "https://example.org")
        self.assertNotEqual(community.monitor_id("https://example.org"), community.monitor_id("http://example.org"))

    def test_passes_resume_from_the_last_confirmed_send(self):
        now = 1_800_000_000.0
        # Nothing confirmed yet: the last two hours.
        self.assertEqual(community.passes_since({}, now), now - 2 * 3600)
        self.assertEqual(community.passes_since({"community_passes_acked_ts": "garbage"}, now), now - 2 * 3600)
        # Confirmed ten minutes ago: two hours of overlap before that.
        self.assertEqual(community.passes_since({"community_passes_acked_ts": str(now - 600)}, now), now - 600 - 2 * 3600)
        # Confirmed two days ago (the box was off): everything since then.
        self.assertEqual(community.passes_since({"community_passes_acked_ts": str(now - 2 * 86400)}, now), now - 2 * 86400 - 2 * 3600)
        # Confirmed a month ago: capped at a week.
        self.assertEqual(community.passes_since({"community_passes_acked_ts": str(now - 30 * 86400)}, now), now - 7 * 86400)

    def test_share_passes_confirms_the_send_only_when_every_batch_went_through(self):
        keys = {"community_enabled": "true", "community_last_id": "abc123"}
        asked = []
        community.db = type("FakeDB", (), {
            "get_all_admin_settings": lambda self: dict(keys),
            "get_admin_setting": lambda self, k, d=None: keys.get(k, d),
            "set_admin_setting": lambda self, k, v: keys.__setitem__(k, v),
            "get_passes_for_sharing": lambda self, since: asked.append(since) or [{"icao24": "484a2b", "timestamp": since + 1, "bucket_5m": 1, "spl_max_db": 60, "spl_db": 55}],
        })()
        self.addCleanup(lambda: setattr(community, "db", self._real_db))
        os.environ.pop("COMMUNITY_SKIP_REMOTE", None)
        self.addCleanup(lambda: os.environ.__setitem__("COMMUNITY_SKIP_REMOTE", "1"))
        original = community.requests.post
        self.addCleanup(lambda: setattr(community.requests, "post", original))

        class Down:
            ok = False
            status_code = 502

        class Up:
            ok = True
            status_code = 200

        community.requests.post = lambda *a, **k: Down()
        self.assertFalse(community.share_passes(force=True)["ok"])
        self.assertNotIn("community_passes_acked_ts", keys)
        community.requests.post = lambda *a, **k: Up()
        self.assertTrue(community.share_passes(force=True)["ok"])
        self.assertAlmostEqual(float(keys["community_passes_acked_ts"]), time.time(), delta=5)
        # The failed run asked for the last two hours; the next run starts two hours before the confirmed send.
        self.assertAlmostEqual(asked[0], time.time() - 2 * 3600, delta=5)
        self.assertAlmostEqual(community.passes_since(keys), time.time() - 2 * 3600, delta=5)

    def test_local_copy_keeps_own_record_until_the_shared_list_has_it(self):
        record, _ = community.build_record(self._settings())
        community.merge_local(record)
        with open(self.tmp.name, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["monitors"][0]["id"], record["id"])
        self.assertNotIn("69616", json.dumps(saved))
        community.remove_local(record["id"])
        with open(self.tmp.name, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["monitors"], [])

    def test_unchanged_record_is_re_sent_once_a_day(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.assertTrue(community._report_is_stale(""))
        self.assertTrue(community._report_is_stale("not a date"))
        self.assertFalse(community._report_is_stale((now - timedelta(hours=2)).replace(microsecond=0).isoformat()))
        self.assertTrue(community._report_is_stale((now - timedelta(hours=25)).replace(microsecond=0).isoformat()))
        self.assertTrue(community._report_is_stale((now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")))

    def test_station_key_replacement_is_validated(self):
        keys = {}
        community.db = type("FakeDB", (), {
            "get_admin_setting": lambda self, k, d=None: keys.get(k, d),
            "set_admin_setting": lambda self, k, v: keys.__setitem__(k, v),
            "get_all_admin_settings": lambda self: dict(keys),
        })()
        self.addCleanup(lambda: setattr(community, "db", self._real_db))
        self.assertFalse(community.set_station_key("short"))
        self.assertFalse(community.set_station_key("x" * 20 + " " + "x" * 20))
        self.assertTrue(community.set_station_key("a" * 48))
        self.assertEqual(keys["api_secret.community_station_key"], "a" * 48)
        result = community.claim_station("ABCD1234")
        self.assertEqual(result["error"], "not_shared")

    def test_shared_list_drops_bad_positions(self):
        record, _ = community.build_record(self._settings())
        record["latitude"] = 999
        cleaned = community._clean_document({"monitors": [record], "last_updated": None})
        self.assertEqual(cleaned["monitors"], [])

if __name__ == "__main__":
    unittest.main()
