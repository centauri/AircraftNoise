import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

import proxy_server
from database import PlaneNoiseDB

WX_COLUMNS = (
    "wx_wind_speed",
    "wx_wind_from_deg",
    "wx_rain",
    "wx_temp_c",
    "wx_temp_2m_c",
    "wx_temp_80m_c",
    "wx_lat",
    "wx_lon",
)

# One 5-minute bucket so later samples conflict with the first insert.
TS = 1_760_000_100


def _weather(**overrides):
    fields = {
        "wx_wind_speed": 6.5,
        "wx_wind_from_deg": 240.0,
        "wx_rain": 1,
        "wx_temp_c": 14.0,
        "wx_temp_2m_c": 13.5,
        "wx_temp_80m_c": 12.25,
        "wx_lat": 52.5,
        "wx_lon": 4.75,
    }
    fields.update(overrides)
    return fields


class StorageTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def _row(self, icao24, event_type="pass"):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM aircraft_events WHERE icao24 = ? AND type = ?",
                (icao24, event_type),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        return dict(rows[0])

    def _assert_weather(self, row, expected):
        for key, value in expected.items():
            if isinstance(value, float):
                self.assertAlmostEqual(row[key], value, places=5, msg=key)
            else:
                self.assertEqual(row[key], value, key)

    def _log(self, icao24, **extra):
        payload = {
            "ts": TS,
            "type": "pass",
            "icao24": icao24,
            "callsign": "TST1",
            "distance_km": 4.0,
            "spl_db": 70.0,
            "latitude": 52.30,
            "longitude": 4.70,
        }
        payload.update(extra)
        self.db.log_event(payload)

    def test_new_pass_stores_supplied_weather(self):
        weather = _weather()
        self._log("newpass", **weather)
        row = self._row("newpass")
        self._assert_weather(row, weather)
        self.assertAlmostEqual(row["spl_db"], 70.0, places=5)

    def test_quieter_sample_keeps_stored_weather(self):
        original = _weather()
        self._log("quiet1", spl_db=80.0, **original)
        self._log(
            "quiet1",
            spl_db=62.0,
            distance_km=3.5,
            **_weather(
                wx_wind_speed=1.0,
                wx_wind_from_deg=10.0,
                wx_rain=0,
                wx_temp_c=1.0,
                wx_temp_2m_c=1.0,
                wx_temp_80m_c=1.0,
                wx_lat=51.0,
                wx_lon=3.0,
            ),
        )
        row = self._row("quiet1")
        self._assert_weather(row, original)
        self.assertAlmostEqual(row["spl_db"], 80.0, places=5)

    def test_louder_sample_with_position_replaces_weather(self):
        self._log("loud1", spl_db=70.0, **_weather())
        replacement = _weather(
            wx_wind_speed=12.0,
            wx_wind_from_deg=90.0,
            wx_rain=0,
            wx_temp_c=18.0,
            wx_temp_2m_c=17.5,
            wx_temp_80m_c=16.0,
            wx_lat=52.11,
            wx_lon=4.22,
        )
        self._log("loud1", spl_db=88.0, **replacement)
        row = self._row("loud1")
        self._assert_weather(row, replacement)
        self.assertAlmostEqual(row["spl_db"], 88.0, places=5)

    def test_spl_only_update_keeps_weather(self):
        weather = _weather()
        self._log("splonly", spl_db=70.0, **weather)
        row = self._row("splonly")
        updated = self.db.update_pass_metrics(row["id"], spl_db=91.0)
        self.assertTrue(updated)
        stored = self._row("splonly")
        self._assert_weather(stored, weather)
        self.assertAlmostEqual(stored["spl_db"], 91.0, places=5)

    def test_closer_quieter_sample_moves_map_pin_only(self):
        weather = _weather()
        self._log(
            "closer1",
            spl_db=82.0,
            distance_km=5.0,
            latitude=52.50,
            longitude=4.70,
            **weather,
        )
        self._log(
            "closer1",
            spl_db=60.0,
            distance_km=1.2,
            latitude=52.41,
            longitude=4.81,
            **_weather(
                wx_wind_speed=2.0,
                wx_wind_from_deg=30.0,
                wx_rain=0,
                wx_temp_c=9.0,
                wx_temp_2m_c=8.5,
                wx_temp_80m_c=7.0,
                wx_lat=51.2,
                wx_lon=3.4,
            ),
        )
        row = self._row("closer1")
        self.assertAlmostEqual(row["latitude"], 52.41, places=5)
        self.assertAlmostEqual(row["longitude"], 4.81, places=5)
        self.assertAlmostEqual(row["distance_km"], 1.2, places=5)
        self.assertAlmostEqual(row["spl_db"], 82.0, places=5)
        self._assert_weather(row, weather)

    def test_row_without_weather_reads_back_null(self):
        self._log("plain1", spl_db=55.0)
        row = self._row("plain1")
        for column in WX_COLUMNS:
            self.assertIsNone(row[column], column)


AMS = ZoneInfo("Europe/Amsterdam")
AIRCRAFT_LAT = 52.40
AIRCRAFT_LON = 4.90
SENSOR_LAT = 53.25
SENSOR_LON = 5.5
TEST_OWM_KEY = "unit-test-key"


def _epoch(hour, minute):
    return datetime(2026, 10, 19, hour, minute, tzinfo=AMS).timestamp()


def _hourly(rows):
    return {
        "hourly": {
            "time": [row[0] for row in rows],
            "temperature_2m": [row[1] for row in rows],
            "temperature_80m": [row[2] for row in rows],
        }
    }


class _Response:
    def __init__(self, status_code, payload, url):
        self.status_code = status_code
        self._payload = payload
        self.url = url
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self.db.set_admin_setting("latitude", "52.40")
        self.db.set_admin_setting("longitude", "4.90")
        self.db.set_admin_setting("radius_km", "8")
        self.db.set_admin_setting("sensor_lat", str(SENSOR_LAT))
        self.db.set_admin_setting("sensor_lon", str(SENSOR_LON))
        self.db.set_admin_setting("site_timezone", "Europe/Amsterdam")

        self._orig_db = proxy_server.db
        self._orig_get = proxy_server.requests.get
        self._orig_meta = proxy_server._get_aircraft_meta
        self._orig_dest = proxy_server.get_aircraft_destination
        self._orig_time = proxy_server.time.time
        self._orig_noise = list(proxy_server.noise_data_history)
        self._orig_latest = proxy_server.latest_noise_data
        self._orig_cache = dict(proxy_server._api_cache)
        self._orig_rates = {key: list(value) for key, value in proxy_server._rate_limit_trackers.items()}
        self._saved_owm_key = proxy_server.CREDS.get("openweather_key")

        proxy_server.db = self.db
        proxy_server._get_aircraft_meta = lambda icao24: None
        proxy_server.get_aircraft_destination = lambda icao24, callsign=None: None
        proxy_server.noise_data_history = []
        proxy_server.latest_noise_data = None
        proxy_server._api_cache.clear()
        proxy_server._rate_limit_trackers.clear()
        proxy_server.CREDS["openweather_key"] = TEST_OWM_KEY
        proxy_server.requests.get = self._network_disabled
        self.calls = {"owm": [], "meteo": []}
        self._clock = None
        self._reset_station_cache()

    def tearDown(self):
        proxy_server.db = self._orig_db
        proxy_server.requests.get = self._orig_get
        proxy_server._get_aircraft_meta = self._orig_meta
        proxy_server.get_aircraft_destination = self._orig_dest
        proxy_server.time.time = self._orig_time
        proxy_server.noise_data_history = self._orig_noise
        proxy_server.latest_noise_data = self._orig_latest
        proxy_server._api_cache.clear()
        proxy_server._api_cache.update(self._orig_cache)
        proxy_server._rate_limit_trackers.clear()
        proxy_server._rate_limit_trackers.update(self._orig_rates)
        if self._saved_owm_key is None:
            proxy_server.CREDS.pop("openweather_key", None)
        else:
            proxy_server.CREDS["openweather_key"] = self._saved_owm_key
        self._reset_station_cache()
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def _reset_station_cache(self):
        cache = getattr(proxy_server, "_station_snapshot_cache", None)
        if isinstance(cache, dict):
            cache["at"] = None
            cache["surface"] = None
            cache["hourly"] = None

    def _network_disabled(self, *args, **kwargs):
        raise AssertionError("network disabled")

    def _install_transport(self, owm_body, meteo_body, owm_error=None, meteo_status=200):
        def fake_get(url, **kwargs):
            host = urlparse(url).netloc
            if host.endswith("openweathermap.org"):
                self.calls["owm"].append((url, kwargs))
                if owm_error is not None:
                    raise owm_error
                return _Response(200, owm_body, url)
            if host.endswith("open-meteo.com"):
                self.calls["meteo"].append((url, kwargs))
                return _Response(meteo_status, meteo_body, url)
            raise AssertionError("unexpected host")

        proxy_server.requests.get = fake_get

    def _freeze(self, now):
        self._clock = _Clock(now)
        proxy_server.time.time = self._clock

    def _aircraft(self, icao24, stamp):
        return {
            "icao24": icao24,
            "callsign": "TST1",
            "latitude": AIRCRAFT_LAT,
            "longitude": AIRCRAFT_LON,
            "geo_altitude": 400,
            "velocity": 60,
            "true_track": 180,
            "last_contact": stamp,
            "source": "opensky",
        }

    def _poll(self, states):
        proxy_server.process_aircraft_states(states, 8.0, 2000.0)

    def _row(self, icao24, event_type):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM aircraft_events WHERE icao24 = ? AND type = ?",
                (icao24, event_type),
            ).fetchall()
        self.assertEqual(len(rows), 1, event_type)
        return dict(rows[0])

    def _assert_null_weather(self, row):
        for column in WX_COLUMNS:
            self.assertIsNone(row[column], column)

    def _owm_query(self, index=0):
        url, kwargs = self.calls["owm"][index]
        query = {key: values[-1] for key, values in parse_qs(urlparse(url).query).items()}
        for key, value in (kwargs.get("params") or {}).items():
            query[str(key)] = str(value)
        return query

    def _assert_sensor_request(self):
        self.assertGreaterEqual(len(self.calls["owm"]), 1)
        query = self._owm_query()
        self.assertAlmostEqual(float(query["lat"]), SENSOR_LAT, places=4)
        self.assertAlmostEqual(float(query["lon"]), SENSOR_LON, places=4)
        self.assertEqual(query.get("units"), "metric")
        self.assertTrue(query.get("appid") == TEST_OWM_KEY, "OpenWeatherMap call did not use the unit-test key")
        self.assertNotAlmostEqual(float(query["lat"]), AIRCRAFT_LAT, places=2)
        self.assertEqual(len(self.calls["meteo"]), len(self.calls["owm"]))
        url, kwargs = self.calls["meteo"][0]
        self.assertTrue(urlparse(url).netloc.endswith("open-meteo.com"))
        params = kwargs.get("params") or {}
        hourly = params.get("hourly")
        if isinstance(hourly, (list, tuple)):
            hourly = ",".join(str(item) for item in hourly)
        hourly = str(hourly)
        self.assertIn("temperature_2m", hourly)
        self.assertIn("temperature_80m", hourly)
        self.assertEqual(str(params.get("timezone")), "Europe/Amsterdam")
        self.assertAlmostEqual(float(params["latitude"]), SENSOR_LAT, places=4)
        self.assertAlmostEqual(float(params["longitude"]), SENSOR_LON, places=4)

    def _surface(self, rain=None):
        body = {
            "main": {"temp": 11.5},
            "wind": {"speed": 6.2, "deg": 200},
        }
        if rain is not None:
            body["rain"] = rain
        return body

    def test_disabled_feature_skips_the_snapshot_and_writes_the_pass(self):
        meteo = _hourly([("2026-10-19T16:00", 12.5, 11.25)])
        self._install_transport(self._surface({"1h": 1.4}), meteo)
        self.db.set_admin_setting("weather_advice_enabled", "false")
        self._freeze(_epoch(16, 50))
        self._poll([self._aircraft("wxoff", _epoch(16, 40))])
        self._clock.now += 400
        self._poll([self._aircraft("wxoff2", _epoch(16, 46))])

        self.assertEqual(self.calls["owm"], [])
        self.assertEqual(self.calls["meteo"], [])
        for icao in ("wxoff", "wxoff2"):
            self._assert_null_weather(self._row(icao, "pass"))

    def test_two_passes_share_one_snapshot(self):
        meteo = _hourly([
            ("2026-10-19T14:00", 0.25, 0.5),
            ("2026-10-19T16:00", 12.5, 11.25),
        ])
        self._install_transport(self._surface({"1h": 1.4}), meteo)
        self._freeze(_epoch(16, 50))
        self._poll([self._aircraft("share1", _epoch(16, 40))])
        self._clock.now += 120
        self._poll([self._aircraft("share2", _epoch(16, 45))])

        self.assertEqual(len(self.calls["owm"]), 1)
        self.assertEqual(len(self.calls["meteo"]), 1)
        self._assert_sensor_request()
        for icao in ("share1", "share2"):
            for event_type in ("pass", "closest_approach"):
                row = self._row(icao, event_type)
                self.assertAlmostEqual(row["wx_wind_speed"], 6.2, places=5)
                self.assertAlmostEqual(row["wx_wind_from_deg"], 200.0, places=5)
                self.assertEqual(row["wx_rain"], 1)
                self.assertAlmostEqual(row["wx_temp_c"], 11.5, places=5)
                self.assertAlmostEqual(row["wx_temp_2m_c"], 12.5, places=5)
                self.assertAlmostEqual(row["wx_temp_80m_c"], 11.25, places=5)
                self.assertAlmostEqual(row["wx_lat"], AIRCRAFT_LAT, places=5)
                self.assertAlmostEqual(row["wx_lon"], AIRCRAFT_LON, places=5)

    def test_new_hour_uses_cached_hourly_pair(self):
        meteo = _hourly([
            ("2026-10-19T14:00", 0.25, 0.5),
            ("2026-10-19T16:00", 8.5, 7.25),
            ("2026-10-19T17:00", 4.0, 6.5),
            ("2026-10-19T18:00", 3.0, 3.0),
        ])
        self._install_transport(self._surface({"1h": 0.2}), meteo)
        self._freeze(_epoch(16, 50))
        self._poll([self._aircraft("hour16", _epoch(16, 58))])
        self._clock.now += 120
        self._poll([self._aircraft("hour17", _epoch(17, 2))])

        self.assertEqual(len(self.calls["owm"]), 1)
        self.assertEqual(len(self.calls["meteo"]), 1)
        first = self._row("hour16", "pass")
        second = self._row("hour17", "pass")
        self.assertAlmostEqual(first["wx_temp_2m_c"], 8.5, places=5)
        self.assertAlmostEqual(first["wx_temp_80m_c"], 7.25, places=5)
        self.assertAlmostEqual(second["wx_temp_2m_c"], 4.0, places=5)
        self.assertAlmostEqual(second["wx_temp_80m_c"], 6.5, places=5)

    def test_missing_rain_object_stores_not_falling(self):
        meteo = _hourly([
            ("2026-10-19T14:00", 0.25, 0.5),
            ("2026-10-19T16:00", 12.5, 11.25),
        ])
        self._install_transport(self._surface(), meteo)
        self._poll([self._aircraft("norain", _epoch(16, 40))])
        row = self._row("norain", "pass")
        self.assertEqual(row["wx_rain"], 0)
        self.assertAlmostEqual(row["wx_temp_c"], 11.5, places=5)
        self.assertAlmostEqual(row["wx_wind_speed"], 6.2, places=5)
        self.assertAlmostEqual(row["wx_temp_2m_c"], 12.5, places=5)

    def test_missing_openweather_key_stores_no_weather(self):
        meteo = _hourly([("2026-10-19T16:00", 12.5, 11.25)])
        self._install_transport(self._surface({"1h": 1.4}), meteo)
        proxy_server.CREDS.pop("openweather_key", None)
        self._poll([
            self._aircraft("nokey1", _epoch(16, 40)),
            self._aircraft("nokey2", _epoch(16, 41)),
        ])
        self.assertEqual(self.calls["owm"], [])
        self.assertEqual(self.calls["meteo"], [])
        for icao in ("nokey1", "nokey2"):
            row = self._row(icao, "pass")
            self._assert_null_weather(row)

    def test_failed_openweather_is_remembered_and_pass_is_written(self):
        meteo = _hourly([("2026-10-19T16:00", 12.5, 11.25)])
        self._install_transport(
            self._surface({"1h": 1.4}),
            meteo,
            owm_error=requests.Timeout("timed out"),
        )
        self._freeze(_epoch(16, 50))
        self._poll([
            self._aircraft("fail1", _epoch(16, 40)),
            self._aircraft("fail2", _epoch(16, 41)),
        ])
        self._clock.now += 299
        self._poll([self._aircraft("fail3", _epoch(16, 42))])

        self.assertEqual(len(self.calls["owm"]), 1)
        for icao in ("fail1", "fail2", "fail3"):
            row = self._row(icao, "pass")
            self._assert_null_weather(row)

    def test_failed_open_meteo_keeps_surface_and_empty_heights(self):
        self._install_transport(self._surface({"1h": 1.4}), {"error": True}, meteo_status=500)
        self._poll([self._aircraft("nometeo", _epoch(16, 40))])
        row = self._row("nometeo", "pass")
        self.assertAlmostEqual(row["wx_wind_speed"], 6.2, places=5)
        self.assertAlmostEqual(row["wx_wind_from_deg"], 200.0, places=5)
        self.assertEqual(row["wx_rain"], 1)
        self.assertAlmostEqual(row["wx_temp_c"], 11.5, places=5)
        self.assertIsNone(row["wx_temp_2m_c"])
        self.assertIsNone(row["wx_temp_80m_c"])
        self.assertAlmostEqual(row["wx_lat"], AIRCRAFT_LAT, places=5)
        self.assertAlmostEqual(row["wx_lon"], AIRCRAFT_LON, places=5)

    def test_height_pair_matches_stamp_hour_not_first_hour(self):
        meteo = _hourly([
            ("2026-10-19T15:00", 1.0, 2.0),
            ("2026-10-19T16:00", 9.5, 10.5),
            ("2026-10-19T18:00", 3.0, 4.0),
        ])
        self._install_transport(self._surface(), meteo)
        self._poll([self._aircraft("midhour", _epoch(16, 10))])
        row = self._row("midhour", "pass")
        self.assertAlmostEqual(row["wx_temp_2m_c"], 9.5, places=5)
        self.assertAlmostEqual(row["wx_temp_80m_c"], 10.5, places=5)
        self.assertNotAlmostEqual(row["wx_temp_2m_c"], 1.0, places=5)

    def test_louder_sample_replaces_weather_from_snapshot(self):
        now = self._orig_time()
        self.db.log_event({
            "ts": now,
            "type": "pass",
            "icao24": "loudwx",
            "callsign": "TST1",
            "distance_km": 4.0,
            "spl_db": 40.0,
            "latitude": AIRCRAFT_LAT,
            "longitude": AIRCRAFT_LON,
            "wx_wind_speed": 1.0,
            "wx_wind_from_deg": 10.0,
            "wx_rain": 0,
            "wx_temp_c": 1.0,
            "wx_temp_2m_c": 1.0,
            "wx_temp_80m_c": 1.0,
            "wx_lat": 51.0,
            "wx_lon": 3.0,
        })
        proxy_server.noise_data_history = [
            {"timestamp": now - 20, "value": 45.0},
            {"timestamp": now, "value": 77.0},
            {"timestamp": now + 20, "value": 45.0},
        ]
        # The stamp is the live clock, so the cached day has to contain that hour.
        meteo = _hourly([
            (f"2026-10-19T{hour:02d}:00", float(hour), float(hour) + 0.5)
            for hour in range(24)
        ])
        self._install_transport(self._surface({"1h": 1.4}), meteo)
        self._poll([self._aircraft("loudwx", now)])
        row = self._row("loudwx", "pass")
        self.assertGreater(row["spl_db"], 40.0)
        self.assertAlmostEqual(row["wx_wind_speed"], 6.2, places=5)
        self.assertEqual(row["wx_rain"], 1)
        self.assertAlmostEqual(row["wx_lat"], AIRCRAFT_LAT, places=5)
        self.assertAlmostEqual(row["wx_lon"], AIRCRAFT_LON, places=5)
        self.assertEqual(len(self.calls["owm"]), 1)
        self.assertEqual(len(self.calls["meteo"]), 1)

    def test_spl_only_recalc_does_not_fetch_snapshot(self):
        now = self._orig_time()
        self.db.log_event({
            "ts": now,
            "type": "pass",
            "icao24": "recalc1",
            "callsign": "TST1",
            "distance_km": 1.0,
            "altitude_m": 400,
            "spl_db": 30.0,
            "latitude": AIRCRAFT_LAT,
            "longitude": AIRCRAFT_LON,
            "wx_wind_speed": 3.0,
            "wx_wind_from_deg": 20.0,
            "wx_rain": 0,
            "wx_temp_c": 9.0,
            "wx_temp_2m_c": 8.0,
            "wx_temp_80m_c": 7.0,
            "wx_lat": AIRCRAFT_LAT,
            "wx_lon": AIRCRAFT_LON,
        })
        proxy_server.noise_data_history = [
            {"timestamp": now - 20, "value": 40.0},
            {"timestamp": now, "value": 70.0},
            {"timestamp": now + 20, "value": 40.0},
        ]
        self._install_transport(self._surface({"1h": 1.4}), _hourly([("2026-10-19T16:00", 12.5, 11.25)]))
        proxy_server.recalc_recent_passes_for_noise(window_minutes=15, limit=50)
        self.assertEqual(self.calls["owm"], [])
        self.assertEqual(self.calls["meteo"], [])
        row = self._row("recalc1", "pass")
        self.assertAlmostEqual(row["wx_wind_speed"], 3.0, places=5)
        self.assertAlmostEqual(row["wx_lat"], AIRCRAFT_LAT, places=5)
        self.assertGreater(row["spl_db"], 30.0)


# Default sensor, seeded and used when the admin keys are absent.
SENTENCE_SENSOR_LAT = 52.5154
SENTENCE_SENSOR_LON = 4.7206
# Due south of that sensor: the line from here to the sensor is 0 degrees.
SOUTH_LAT = 52.40
# Due north of that sensor. The map pin sits here so a classifier that uses
# latitude/longitude instead of wx_lat/wx_lon aims the wrong way.
NORTH_LAT = 52.60
# Aircraft at (52.40, 4.70), sensor along the initial bearing of 10 degrees.
WRAP_SENSOR_LAT = 52.47085093911137
WRAP_SENSOR_LON = 4.720508798622518

ENGLISH = {
    "R7": (
        "The wind was blowing from the aircraft toward the sensor. "
        "Downwind, the sound bends downward and is heard more strongly than in calm air."
    ),
    "R8": (
        "The wind was blowing from the sensor toward the aircraft. "
        "Upwind, the sound bends upward, so the aircraft is heard more weakly than in calm air."
    ),
    "R9": (
        "The wind was strong. Strong wind raises the noise around the microphone "
        "and masks the aircraft, and it makes the level unsteady."
    ),
    "R10": (
        "Rain was falling. The measured level includes the rain, so it is not a clean "
        "aircraft level, and the rain can mask the pass."
    ),
    "R11": (
        "The model shows warmer air at 80 m than at the ground. That low inversion "
        "bends sound back down, so the aircraft carries farther than it would in a "
        "normal daytime atmosphere."
    ),
    "R12": (
        "The air was calm and cold. Sound often carries farther in that kind of air. "
        "The model does not show a warmer layer at 80 m."
    ),
}


class SentenceTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self._orig_db = proxy_server.db
        proxy_server.db = self.db
        self.client = proxy_server.app.test_client()
        # The sensor position is an admin setting now, not a code default.
        proxy_server.db.set_admin_setting('sensor_lat', str(SENTENCE_SENSOR_LAT))
        proxy_server.db.set_admin_setting('sensor_lon', str(SENTENCE_SENSOR_LON))

    def tearDown(self):
        proxy_server.db = self._orig_db
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def _pass(self, icao24, **fields):
        payload = {
            "ts": TS,
            "type": "pass",
            "icao24": icao24,
            "callsign": "TST1",
            "distance_km": 4.0,
            "spl_db": 70.0,
            "latitude": NORTH_LAT,
            "longitude": SENTENCE_SENSOR_LON,
            "wx_wind_speed": 3.0,
            "wx_wind_from_deg": 270.0,
            "wx_rain": 0,
            "wx_temp_c": 15.0,
            "wx_temp_2m_c": 14.0,
            "wx_temp_80m_c": 13.0,
            "wx_lat": SOUTH_LAT,
            "wx_lon": SENTENCE_SENSOR_LON,
        }
        payload.update(fields)
        self.db.log_event(payload)

    def _stored(self, icao24):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM aircraft_events WHERE icao24 = ?",
                (icao24,),
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def _item(self, icao24):
        response = self.client.get("/recent_events?limit=50")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        matches = [event for event in body["events"] if event.get("icao24") == icao24]
        self.assertEqual(len(matches), 1)
        return matches[0], response.get_data(as_text=True)

    def _assert_ids(self, item, raw, expected_ids):
        self.assertEqual(item["weather_sentences"], expected_ids)
        self.assertIs(item["weather_mark"], bool(expected_ids))
        selected = [
            proxy_server.WEATHER_SENTENCE_TEXT[sentence_id]
            for sentence_id in item["weather_sentences"]
        ]
        self.assertEqual(selected, [ENGLISH[sentence_id] for sentence_id in expected_ids])
        for sentence in ENGLISH.values():
            self.assertNotIn(sentence, raw)
        self.assertNotIn("wx_wind_speed", item)
        self.assertNotIn("wx_lat", item)
        self.assertNotIn("wx_lon", item)

    def test_ids_select_verbatim_english(self):
        self.assertEqual(proxy_server.WEATHER_SENTENCE_TEXT, ENGLISH)

    def test_disabled_feature_hides_a_stored_note(self):
        self.db.set_admin_setting("weather_advice_enabled", "false")
        self._pass("hidden1", wx_wind_from_deg=180.0)
        item, raw = self._item("hidden1")
        self._assert_ids(item, raw, [])
        self.assertAlmostEqual(self._stored("hidden1")["wx_wind_from_deg"], 180.0, places=5)
        settings = self.client.get("/visitor/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIs(settings.get_json()["weather_advice"], False)

    def test_downwind_yields_r7(self):
        self._pass("down1", wx_wind_from_deg=180.0, spl_db=76.5)
        item, raw = self._item("down1")
        self._assert_ids(item, raw, ["R7"])
        self.assertEqual(item["spl_db"], 76.5)
        self.assertAlmostEqual(item["latitude"], NORTH_LAT, places=5)
        self.assertAlmostEqual(self._stored("down1")["wx_lat"], SOUTH_LAT, places=5)

    def test_upwind_yields_r8(self):
        self._pass("up1", wx_wind_from_deg=0.0)
        item, raw = self._item("up1")
        self._assert_ids(item, raw, ["R8"])

    def test_side_wind_yields_neither_direction(self):
        self._pass("side1", wx_wind_from_deg=270.0)
        item, raw = self._item("side1")
        self._assert_ids(item, raw, [])

    def test_travel_exactly_60_degrees_is_downwind(self):
        # Travel 60, line to the sensor is 0. The shortest arc is 60.
        self._pass("edge60", wx_wind_from_deg=240.0)
        item, raw = self._item("edge60")
        self._assert_ids(item, raw, ["R7"])

    def test_travel_61_degrees_is_neither_direction(self):
        self._pass("edge61", wx_wind_from_deg=241.0)
        item, raw = self._item("edge61")
        self._assert_ids(item, raw, [])

    def test_travel_350_is_within_60_of_line_10(self):
        # Stored wind-from 170 travels toward 350. The line is 10 degrees.
        # Shortest arc is 20, so this is downwind. The return line is not.
        self.db.set_admin_setting("sensor_lat", str(WRAP_SENSOR_LAT))
        self.db.set_admin_setting("sensor_lon", str(WRAP_SENSOR_LON))
        self._pass(
            "wrap350",
            wx_wind_from_deg=170.0,
            wx_lat=52.40,
            wx_lon=4.70,
            latitude=51.0,
            longitude=5.0,
        )
        item, raw = self._item("wrap350")
        self._assert_ids(item, raw, ["R7"])

    def test_wind_of_5_yields_r9_and_4_9_does_not(self):
        # Map pin is due south, wind travels north. No wx position, so R7 stays off.
        self._pass(
            "wind5",
            wx_wind_speed=5.0,
            wx_wind_from_deg=180.0,
            wx_lat=None,
            wx_lon=None,
            wx_temp_2m_c=None,
            wx_temp_80m_c=None,
            latitude=SOUTH_LAT,
            longitude=SENTENCE_SENSOR_LON,
        )
        self._pass(
            "wind49",
            wx_wind_speed=4.9,
            wx_wind_from_deg=180.0,
            wx_lat=None,
            wx_lon=None,
            wx_temp_2m_c=None,
            wx_temp_80m_c=None,
            latitude=SOUTH_LAT,
            longitude=SENTENCE_SENSOR_LON,
        )
        strong, raw = self._item("wind5")
        self._assert_ids(strong, raw, ["R9"])
        calm, raw_calm = self._item("wind49")
        self._assert_ids(calm, raw_calm, [])

    def test_wind_of_2_does_not_yield_r12_and_below_2_can(self):
        shared = {
            "wx_wind_from_deg": 270.0,
            "wx_rain": 0,
            "wx_temp_c": 5.0,
            "wx_temp_2m_c": 4.0,
            "wx_temp_80m_c": 4.0,
            "wx_lat": None,
            "wx_lon": None,
        }
        self._pass("calm2", wx_wind_speed=2.0, **shared)
        self._pass("calm19", wx_wind_speed=1.9, **shared)
        at_two, raw_two = self._item("calm2")
        self._assert_ids(at_two, raw_two, [])
        below, raw_below = self._item("calm19")
        self._assert_ids(below, raw_below, ["R12"])

    def test_ground_of_5_can_yield_r12_and_5_1_cannot(self):
        shared = {
            "wx_wind_speed": 1.0,
            "wx_wind_from_deg": 270.0,
            "wx_rain": 0,
            "wx_temp_2m_c": 4.0,
            "wx_temp_80m_c": 3.0,
            "wx_lat": None,
            "wx_lon": None,
        }
        self._pass("cold5", wx_temp_c=5.0, **shared)
        self._pass("cold51", wx_temp_c=5.1, **shared)
        cold, raw_cold = self._item("cold5")
        self._assert_ids(cold, raw_cold, ["R12"])
        warmer, raw_warmer = self._item("cold51")
        self._assert_ids(warmer, raw_warmer, [])

    def test_inversion_yields_r11_and_omits_r12(self):
        self._pass(
            "inv1",
            wx_wind_speed=1.0,
            wx_temp_c=0.0,
            wx_temp_2m_c=2.0,
            wx_temp_80m_c=3.5,
            wx_lat=None,
            wx_lon=None,
        )
        item, raw = self._item("inv1")
        self._assert_ids(item, raw, ["R11"])

    def test_equal_heights_do_not_yield_r11(self):
        self._pass(
            "eq1",
            wx_wind_speed=1.0,
            wx_temp_c=5.0,
            wx_temp_2m_c=4.0,
            wx_temp_80m_c=4.0,
            wx_lat=None,
            wx_lon=None,
        )
        item, raw = self._item("eq1")
        self._assert_ids(item, raw, ["R12"])

    def test_missing_wind_position_omits_r7_r8_and_can_yield_r9(self):
        self._pass(
            "nopos",
            wx_wind_speed=6.0,
            wx_wind_from_deg=180.0,
            wx_lat=None,
            wx_lon=None,
            latitude=SOUTH_LAT,
            longitude=SENTENCE_SENSOR_LON,
        )
        item, raw = self._item("nopos")
        self._assert_ids(item, raw, ["R9"])

    def test_missing_heights_omit_r11_r12_and_keep_direction(self):
        # Calm and cold would be R12 if the height pair were present.
        self._pass(
            "noh1",
            wx_wind_speed=1.0,
            wx_wind_from_deg=180.0,
            wx_temp_c=0.0,
            wx_temp_2m_c=None,
            wx_temp_80m_c=None,
        )
        self._pass(
            "noh2",
            wx_wind_speed=1.0,
            wx_wind_from_deg=180.0,
            wx_rain=1,
            wx_temp_c=0.0,
            wx_temp_2m_c=1.0,
            wx_temp_80m_c=None,
        )
        both_missing, raw_both = self._item("noh1")
        self._assert_ids(both_missing, raw_both, ["R7"])
        one_missing, raw_one = self._item("noh2")
        self._assert_ids(one_missing, raw_one, ["R7", "R10"])

    def test_rain_and_downwind_yield_both_and_mark(self):
        self._pass(
            "raindown",
            spl_db=88.0,
            wx_wind_speed=3.0,
            wx_wind_from_deg=180.0,
            wx_rain=1,
            wx_temp_c=15.0,
            wx_temp_2m_c=14.0,
            wx_temp_80m_c=13.0,
        )
        item, raw = self._item("raindown")
        self._assert_ids(item, raw, ["R7", "R10"])
        self.assertIs(item["weather_mark"], True)
        self.assertEqual(item["spl_db"], 88.0)
        self.assertEqual(self._stored("raindown")["spl_db"], 88.0)

    def test_missing_or_zero_rain_omits_r10(self):
        self._pass("rain0", wx_wind_from_deg=180.0, wx_rain=0)
        self._pass("rainnone", wx_wind_from_deg=180.0, wx_rain=None)
        dry, raw_dry = self._item("rain0")
        self._assert_ids(dry, raw_dry, ["R7"])
        missing, raw_missing = self._item("rainnone")
        self._assert_ids(missing, raw_missing, ["R7"])

    def test_no_applicable_sentence_is_unmarked(self):
        self._pass("none1")
        item, raw = self._item("none1")
        self._assert_ids(item, raw, [])
        self.assertIs(item["weather_mark"], False)

    def test_row_without_weather_is_unmarked_and_keeps_spl(self):
        self.db.log_event({
            "ts": TS,
            "type": "pass",
            "icao24": "plain1",
            "callsign": "TST1",
            "distance_km": 4.0,
            "spl_db": 73.4,
            "latitude": SOUTH_LAT,
            "longitude": SENTENCE_SENSOR_LON,
        })
        item, raw = self._item("plain1")
        self._assert_ids(item, raw, [])
        self.assertIs(item["weather_mark"], False)
        self.assertEqual(item["spl_db"], 73.4)
        self.assertEqual(item["icao24"], "plain1")
        self.assertIn("latitude", item)
        stored = self._stored("plain1")
        self.assertEqual(stored["spl_db"], 73.4)
        for column in WX_COLUMNS:
            self.assertIsNone(stored[column], column)
        self.assertNotIn("weather_sentences", stored)
        self.assertNotIn("weather_mark", stored)
        with sqlite3.connect(self.path) as conn:
            names = [row[1] for row in conn.execute("PRAGMA table_info(aircraft_events)")]
        self.assertNotIn("weather_sentences", names)
        self.assertNotIn("weather_mark", names)
        detail = self.client.get(f"/api/event/{stored['id']}")
        self.assertEqual(detail.status_code, 200)
        event = detail.get_json()["event"]
        self.assertNotIn("weather_mark", event)
        self.assertNotIn("weather_sentences", event)
        self.assertEqual(event["spl_db"], 73.4)
        for sentence in ENGLISH.values():
            self.assertNotIn(sentence, raw)


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "index.html"), encoding="utf-8") as handle:
            cls.page = handle.read()
        with open(os.path.join(root, "static/js/translations.js"), encoding="utf-8") as handle:
            cls.translations = handle.read()
        with open(os.path.join(root, "admin_settings.html"), encoding="utf-8") as handle:
            cls.settings_page = handle.read()

    def _language_blocks(self):
        en_at = self.translations.find("\n  en: {")
        nl_at = self.translations.find("\n  nl: {")
        self.assertGreater(en_at, -1)
        self.assertGreater(nl_at, en_at)
        return self.translations[en_at:nl_at], self.translations[nl_at:]

    def _keyed_sentence(self, block, sentence_id):
        match = re.search(
            rf"weather_{sentence_id}:\s*\"((?:\\.|[^\"\\])*)\"",
            block,
        )
        self.assertIsNotNone(match, sentence_id)
        return match.group(1)

    def test_page_binds_mark_passes_ids_and_credits_open_meteo(self):
        self.assertIn("Open-Meteo", self.page)
        self.assertIn("weather-mark", self.page)
        self.assertRegex(self.page, r"window\.t\(\s*['\"]weather_['\"]\s*\+")
        self.assertIn("_sensorMarker.getLatLng", self.page)
        # Only the privacy scatter moves the sensor ring; the weather code never does.
        weather_part = self.page[self.page.index("function eventFocusPopupHtml"):self.page.index("function focusOnEvent")]
        self.assertNotIn("_sensorMarker.setLatLng", weather_part)
        self.assertIn("weather_advice", self.page)
        self.assertIn('id="weatherAdviceEnabled"', self.settings_page)
        self.assertIn("weather_advice_enabled", self.settings_page)
        calls = re.findall(r"focusOnEvent\(\{([^}]+)\}\)", self.page, re.S)
        self.assertEqual(len(calls), 2)
        for body in calls:
            self.assertIn("weather_sentences", body)

    def test_translations_key_six_sentences_by_id_and_language(self):
        en_block, nl_block = self._language_blocks()
        self.assertEqual(set(proxy_server.WEATHER_SENTENCE_TEXT), {
            "R7", "R8", "R9", "R10", "R11", "R12",
        })
        for sentence_id, english in proxy_server.WEATHER_SENTENCE_TEXT.items():
            self.assertEqual(self._keyed_sentence(en_block, sentence_id), english)
            dutch = self._keyed_sentence(nl_block, sentence_id)
            self.assertTrue(dutch.strip())
            self.assertNotEqual(dutch, english)
            self.assertIn(dutch, nl_block)
            self.assertNotIn(english, nl_block)

    def test_dutch_sentence_for_an_id_differs_and_lives_in_the_dutch_table(self):
        en_block, nl_block = self._language_blocks()
        english = self._keyed_sentence(en_block, "R7")
        dutch = self._keyed_sentence(nl_block, "R7")
        self.assertEqual(english, proxy_server.WEATHER_SENTENCE_TEXT["R7"])
        self.assertNotEqual(dutch, english)
        self.assertIn(dutch, nl_block)
        self.assertNotIn(dutch, en_block)

    def test_false_weather_mark_does_not_take_the_mark_branch(self):
        match = re.search(r"if\s*\(\s*(event\.weather_mark)\s*\)", self.page)
        self.assertIsNotNone(match)
        condition = match.group(0)
        self.assertEqual(condition, "if (event.weather_mark)")
        self.assertNotIn("!", condition)
        branch = self.page[match.start():self.page.find("}", match.start())]
        self.assertIn("weather-mark", branch)
        self.assertIn(
            "const noise = event.spl_db ? `${event.spl_db.toFixed(1)} dB` : 'N/A';",
            self.page,
        )


if __name__ == "__main__":
    unittest.main()
