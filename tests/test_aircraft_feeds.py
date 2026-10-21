import os
import sqlite3
import tempfile
import unittest

import proxy_server
from database import PlaneNoiseDB


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self.headers = {}
        self.text = ""
        self._body = body

    def json(self):
        return self._body


def _state():
    return [
        "abc123", "TST1", "Netherlands", 1, 2,
        4.72, 52.52, 100.0, False, 60.0, 180.0, 0.0,
        None, 110.0, None, False, 0,
    ]


class AircraftFeedTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self.db.set_admin_setting("radius_km", "8")
        self.db.set_admin_setting("multi_api_multi_api", "true")
        self.db.set_admin_setting("active_aircraft_feed", "multi")
        self.db.set_admin_setting("aircraft_base_url", "https://api.airplanes.live/v2")
        self.db.set_admin_setting("aircraft_poll_interval_seconds", "10")

        self._orig = {
            "db": proxy_server.db,
            "get": proxy_server.requests.get,
            "token": proxy_server.get_oauth_token,
            "cache": dict(proxy_server._token_cache),
            "creds": dict(proxy_server.CREDS),
            "fetchers": dict(proxy_server.AIRCRAFT_FETCHERS),
            "configs": dict(proxy_server.API_CONFIGS),
            "failed": dict(proxy_server._feed_failed),
            "meta_gone": proxy_server._opensky_meta_gone,
            "meta_cache": dict(proxy_server._air_meta_cache),
            "enrich": dict(proxy_server._fa_enrich_cache),
            "enrich_next": proxy_server._fa_enrich_next_at,
        }
        proxy_server.db = self.db
        proxy_server._feed_failed.clear()
        proxy_server._token_cache["access_token"] = None
        proxy_server._token_cache["expires_at"] = 0
        proxy_server.CREDS["clientId"] = "client"
        proxy_server.CREDS["clientSecret"] = "secret"
        proxy_server.CREDS["flightaware_key"] = "fa-key"
        proxy_server.get_oauth_token = lambda cid, sec: "token-1"
        proxy_server._opensky_meta_gone = False
        proxy_server._air_meta_cache.clear()
        proxy_server._fa_enrich_cache.clear()
        proxy_server._fa_enrich_next_at = 0.0
        proxy_server.load_api_configs_from_database()

    def tearDown(self):
        proxy_server.db = self._orig["db"]
        proxy_server.requests.get = self._orig["get"]
        proxy_server.get_oauth_token = self._orig["token"]
        proxy_server._token_cache.clear()
        proxy_server._token_cache.update(self._orig["cache"])
        proxy_server.CREDS.clear()
        proxy_server.CREDS.update(self._orig["creds"])
        proxy_server.AIRCRAFT_FETCHERS.clear()
        proxy_server.AIRCRAFT_FETCHERS.update(self._orig["fetchers"])
        proxy_server.API_CONFIGS.clear()
        proxy_server.API_CONFIGS.update(self._orig["configs"])
        proxy_server._feed_failed.clear()
        proxy_server._feed_failed.update(self._orig["failed"])
        proxy_server._opensky_meta_gone = self._orig["meta_gone"]
        proxy_server._air_meta_cache.clear()
        proxy_server._air_meta_cache.update(self._orig["meta_cache"])
        proxy_server._fa_enrich_cache.clear()
        proxy_server._fa_enrich_cache.update(self._orig["enrich"])
        proxy_server._fa_enrich_next_at = self._orig["enrich_next"]
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def test_opensky_sends_the_saved_client_token(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return _Response(200, {"states": [_state()]})

        proxy_server.requests.get = fake_get
        rows = proxy_server.get_aircraft_data_opensky()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["icao24"], "abc123")
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer token-1")
        self.assertIn("opensky-network.org/api/states/all", calls[0][0])
        self.assertIs(proxy_server._feed_failed["aircraft"], False)

    def test_opensky_retries_once_after_401(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(kwargs["headers"].get("Authorization"))
            if len(calls) == 1:
                return _Response(401, {})
            return _Response(200, {"states": []})

        proxy_server.requests.get = fake_get
        rows = proxy_server.get_aircraft_data_opensky()
        self.assertEqual(rows, [])
        self.assertEqual(calls, ["Bearer token-1", "Bearer token-1"])
        self.assertIs(proxy_server._feed_failed["aircraft"], False)

    def test_flightaware_asks_for_positions_inside_the_box(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return _Response(200, {"positions": [{
                "fa_flight_id": "KLM1",
                "ident": "KLM1",
                "latitude": 52.52,
                "longitude": 4.72,
                "altitude": 10,
                "groundspeed": 200,
                "heading": 90,
                "timestamp": "2026-10-19T18:00:00Z",
            }]})

        self.db.set_admin_setting("aircraft_flightaware_enrichment_only", "false")
        proxy_server.load_api_configs_from_database()
        proxy_server.requests.get = fake_get
        rows = proxy_server.get_flightaware_aircraft_data()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["callsign"], "KLM1")
        self.assertAlmostEqual(rows[0]["geo_altitude"], 304.8, places=3)
        self.assertEqual(rows[0]["source"], "flightaware")
        url, kwargs = calls[0]
        self.assertTrue(url.endswith("/flights/search/positions"))
        self.assertEqual(kwargs["headers"]["x-apikey"], "fa-key")
        self.assertEqual(kwargs["params"]["max_pages"], 1)
        self.assertEqual(kwargs["params"]["unique_flights"], "true")
        self.assertNotIn("inAir", kwargs["params"]["query"])
        self.assertIn("{range lat ", kwargs["params"]["query"])

    def test_flightaware_stops_after_the_daily_calls(self):
        self.db.set_admin_setting("aircraft_flightaware_enrichment_only", "false")
        proxy_server.load_api_configs_from_database()
        proxy_server.db.track_api_usage("flightaware", daily_requests=3)
        calls = []
        proxy_server.requests.get = lambda *args, **kwargs: calls.append(1)
        rows = proxy_server.get_flightaware_aircraft_data()
        self.assertEqual(rows, [])
        self.assertEqual(calls, [])
        self.assertIs(proxy_server._feed_failed["aircraft_flightaware"], True)

    def test_empty_opensky_does_not_spend_flightaware(self):
        seen = []

        def opensky():
            seen.append("opensky")
            proxy_server._mark_feed("aircraft", False)
            return []

        def flightaware():
            seen.append("flightaware")
            return [{"icao24": "abc"}]

        proxy_server.AIRCRAFT_FETCHERS["opensky"] = opensky
        proxy_server.AIRCRAFT_FETCHERS["flightaware"] = flightaware
        self.assertEqual(proxy_server.get_aircraft_data_multi_api(), [])
        self.assertEqual(seen, ["opensky"])

    def test_failed_opensky_asks_flightaware_next(self):
        self.db.set_admin_setting("aircraft_flightaware_enrichment_only", "false")
        proxy_server.load_api_configs_from_database()
        seen = []

        def opensky():
            seen.append("opensky")
            proxy_server._mark_feed("aircraft", True)
            return []

        def flightaware():
            seen.append("flightaware")
            proxy_server._mark_feed("aircraft_flightaware", False)
            return [{"icao24": "abc123", "latitude": 1, "longitude": 2}]

        proxy_server.AIRCRAFT_FETCHERS["opensky"] = opensky
        proxy_server.AIRCRAFT_FETCHERS["flightaware"] = flightaware
        rows = proxy_server.get_aircraft_data_multi_api()
        self.assertEqual(seen, ["opensky", "flightaware"])
        self.assertEqual(rows[0]["icao24"], "abc123")

    def test_lead_feed_sets_the_poll_pace(self):
        # No quiet window in this test: the day interval is the floor whatever the clock says.
        self.db.set_admin_setting("poll_quiet_start", "00:00")
        self.db.set_admin_setting("poll_quiet_end", "00:00")
        aircraft, noise = proxy_server._active_poll_intervals()
        self.assertEqual(proxy_server.get_api_config("aircraft")["poll_interval_seconds"], 10)
        self.assertEqual(aircraft, 30)
        self.assertEqual(
            proxy_server.get_api_config("aircraft_airplanes_live")["poll_interval_seconds"],
            10,
        )

    def test_flightaware_spends_when_the_switch_is_on(self):
        self.db.set_admin_setting("aircraft_flightaware_enrichment_only", "false")
        self.db.set_admin_setting("aircraft_flightaware_spend_beyond_credit", "true")
        proxy_server.load_api_configs_from_database()
        proxy_server.db.track_api_usage("flightaware", daily_requests=3)
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response(200, {"positions": []})

        proxy_server.requests.get = fake_get
        rows = proxy_server.get_flightaware_aircraft_data()
        self.assertEqual(rows, [])
        self.assertEqual(len(calls), 1)
        self.assertIs(proxy_server._feed_failed["aircraft_flightaware"], False)

    def test_opensky_metadata_410_is_remembered(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response(410, "")

        proxy_server.requests.get = fake_get
        self.assertIsNone(proxy_server._get_aircraft_meta("485A33"))
        self.assertIsNone(proxy_server._get_aircraft_meta("4cad98"))
        self.assertEqual(len(calls), 1)
        self.assertTrue(proxy_server._opensky_meta_gone)

    def test_opensky_metadata_keeps_make_and_model(self):
        def fake_get(url, **kwargs):
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-1")
            return _Response(200, {
                "manufacturerName": "Airbus",
                "model": "A321-252NX",
                "registration": "PH-AXA",
            })

        proxy_server.requests.get = fake_get
        meta = proxy_server._get_aircraft_meta("485a33")
        self.assertEqual(meta["manufacturerName"], "Airbus")
        self.assertEqual(meta["model"], "A321-252NX")

        def fail_get(*args, **kwargs):
            raise AssertionError("cached")

        proxy_server.requests.get = fail_get
        again = proxy_server._get_aircraft_meta("485A33")
        self.assertEqual(again["model"], "A321-252NX")

    def test_spend_switch_is_on_the_flightaware_card(self):
        from feeds import feed_by_id
        feed = feed_by_id("aircraft_flightaware")
        field = next(item for item in feed["fields"] if item["key"] == "spend_beyond_credit")
        self.assertEqual(field["type"], "boolean")
        self.assertFalse(feed["config"]["spend_beyond_credit"])
        page_path = os.path.join(os.path.dirname(__file__), "..", "admin_api_usage.html")
        with open(page_path, encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn('field.type === "boolean"', page)
        self.assertIn("spend_beyond_credit", page)
        enrich = next(item for item in feed["fields"] if item["key"] == "enrichment_only")
        self.assertEqual(enrich["type"], "boolean")
        self.assertTrue(feed["config"]["enrichment_only"])
        self.assertIn("enrichment_only", page)

    def test_enrichment_only_skips_positions_and_tries_the_next_feed(self):
        self.db.set_admin_setting("multi_api_enable_airplanes_live", "true")
        proxy_server.load_api_configs_from_database()
        seen = []

        def opensky():
            seen.append("opensky")
            proxy_server._mark_feed("aircraft", True)
            return []

        def flightaware():
            seen.append("flightaware")
            return [{"icao24": "abc"}]

        def airplanes():
            seen.append("airplanes")
            proxy_server._mark_feed("aircraft_airplanes_live", False)
            return [{"icao24": "def456", "latitude": 1, "longitude": 2}]

        proxy_server.AIRCRAFT_FETCHERS["opensky"] = opensky
        proxy_server.AIRCRAFT_FETCHERS["flightaware"] = flightaware
        proxy_server.AIRCRAFT_FETCHERS["airplanes_live"] = airplanes
        rows = proxy_server.get_aircraft_data_multi_api()
        self.assertEqual(seen, ["opensky", "airplanes"])
        self.assertEqual(rows[0]["icao24"], "def456")

    def test_enrichment_looks_up_a_callsign_once(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs.get("params")))
            return _Response(200, {"flights": [{
                "ident": "SAS1558",
                "aircraft_type": "A20N",
                "registration": "SE-ROI",
                "operator": "SAS",
                "destination": {"name": "Copenhagen", "code_iata": "CPH"},
            }]})

        proxy_server.requests.get = fake_get
        first = proxy_server._flightaware_enrich("sas1558")
        second = proxy_server._flightaware_enrich("SAS1558")
        self.assertEqual(first["model"], "A20N")
        self.assertEqual(first["registration"], "SE-ROI")
        self.assertEqual(first["destination"], "Copenhagen")
        self.assertEqual(second["model"], "A20N")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("/flights/SAS1558"))
        self.assertEqual(calls[0][1]["max_pages"], 1)

    def test_enrichment_stops_after_thirty_lookups(self):
        proxy_server.db.track_api_usage("flightaware", daily_requests=30)
        calls = []
        proxy_server.requests.get = lambda *args, **kwargs: calls.append(1)
        self.assertIsNone(proxy_server._flightaware_enrich("KLM37Y"))
        self.assertEqual(calls, [])

    def test_enrichment_waits_between_lookups(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response(200, {"flights": [{"aircraft_type": "A20N"}]})

        proxy_server.requests.get = fake_get
        self.assertEqual(proxy_server._flightaware_enrich("SAS1558")["model"], "A20N")
        self.assertIsNone(proxy_server._flightaware_enrich("KLM37Y"))
        self.assertEqual(len(calls), 1)
        proxy_server._fa_enrich_next_at = 1
        self.assertEqual(proxy_server._flightaware_enrich("KLM37Y")["model"], "A20N")
        self.assertEqual(len(calls), 2)

    def test_airplanes_live_reports_feet_and_knots(self):
        proxy_server.requests.get = lambda *args, **kwargs: _Response(200, {"ac": [
            {"hex": "ABC123", "flight": "TST1 ", "lat": 52.52, "lon": 4.72,
             "alt_baro": 9100, "alt_geom": 9200, "gs": 180},
            {"hex": "def456", "flight": "TST2", "lat": 52.50, "lon": 4.70,
             "alt_baro": "ground", "gs": 5},
        ]})
        states = proxy_server.get_aircraft_data_airplanes_live()
        self.assertEqual(len(states), 2)
        self.assertAlmostEqual(states[0]["geo_altitude"], 9200 * 0.3048)
        self.assertAlmostEqual(states[0]["baro_altitude"], 9100 * 0.3048)
        self.assertEqual(states[0]["icao24"], "abc123")
        self.assertEqual(states[1]["geo_altitude"], 0.0)
        self.assertEqual(proxy_server.speed_kmh_from_feed(180, "airplanes_live"), 333)
        self.assertEqual(proxy_server.speed_kmh_from_feed(180, "adsb_fi"), 333)

    def test_adsb_fi_and_adsb_lol_use_the_same_shape_with_their_own_urls(self):
        seen = []

        def fake_get(url, headers=None, timeout=None):
            seen.append(url)
            return _Response(200, {"ac": [{"hex": "ABC123", "flight": "TST1", "lat": 52.5, "lon": 4.7, "alt_baro": 3000, "gs": 150, "track": 42}]})

        proxy_server.requests.get = fake_get
        fi = proxy_server.get_aircraft_data_adsb_fi()
        lol = proxy_server.get_aircraft_data_adsb_lol()
        self.assertEqual(fi[0]["source"], "adsb_fi")
        self.assertEqual(lol[0]["source"], "adsb_lol")
        self.assertEqual(fi[0]["true_track"], 42)
        self.assertTrue(seen[0].startswith("https://opendata.adsb.fi/api/v3/lat/"), seen[0])
        self.assertIn("/lon/", seen[0]); self.assertIn("/dist/", seen[0])
        self.assertTrue(seen[1].startswith("https://api.adsb.lol/v2/point/"), seen[1])
        # The catalog keeps adsb.lol slow: its daily cap allows a poll every three minutes at best.
        from feeds import feed_by_id
        floor = [f for f in feed_by_id("aircraft_adsb_lol")["fields"] if f["key"] == "poll_interval_seconds"][0]["min"]
        self.assertGreaterEqual(floor, 180)
        self.assertEqual(feed_by_id("aircraft_adsb_lol")["usage"]["daily_limit"], 500)
        self.assertIsNone(feed_by_id("aircraft_adsb_fi")["usage"]["daily_limit"])
        self.assertEqual(proxy_server.speed_kmh_from_feed(180, "flightaware"), 333)
        self.assertEqual(proxy_server.speed_kmh_from_feed(100, "opensky"), 360)
        self.assertIsNone(proxy_server.speed_kmh_from_feed(None, "opensky"))
