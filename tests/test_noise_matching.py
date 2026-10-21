import os
import tempfile
import time
import unittest

import proxy_server
from database import PlaneNoiseDB


SENSOR = (52.5154, 4.7087)


def _series(now, peak_at, peak_db=60.0, background=40.0):
    """10 s samples for three minutes around now with one peak."""
    out = []
    for offset in range(-90, 100, 10):
        stamp = now + offset
        value = background
        if abs(stamp - peak_at) < 1:
            value = peak_db
        elif abs(stamp - peak_at) <= 10:
            value = background + 8.0
        out.append({"timestamp": stamp, "value": value, "la_max": value + 5.0})
    return out


class _RecorderFixture(unittest.TestCase):
    """Temp database, stubbed network, one noise peak at `now`."""
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self.db.set_admin_setting("latitude", str(SENSOR[0]))
        self.db.set_admin_setting("longitude", str(SENSOR[1]))
        self.db.set_admin_setting("radius_km", "8")
        self.db.set_admin_setting("noise_assoc_inner_km", "4")
        self.db.set_admin_setting("weather_advice_enabled", "false")
        self._orig = {
            "db": proxy_server.db,
            "noise": proxy_server.noise_data_history,
            "tracks": dict(proxy_server._tracks),
            "meta": proxy_server._get_aircraft_meta,
            "get": proxy_server.requests.get,
        }
        proxy_server.db = self.db
        proxy_server._polling_started = True  # the test client must not start the pollers
        proxy_server._tracks.clear()
        proxy_server._get_aircraft_meta = lambda icao24: None
        proxy_server.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network"))
        self.now = float(int(time.time()))
        proxy_server.noise_data_history = _series(self.now, self.now)

    def tearDown(self):
        proxy_server.db = self._orig["db"]
        proxy_server.noise_data_history = self._orig["noise"]
        proxy_server._tracks.clear()
        proxy_server._tracks.update(self._orig["tracks"])
        proxy_server._get_aircraft_meta = self._orig["meta"]
        proxy_server.requests.get = self._orig["get"]
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def _state(self, icao24, north_km, alt_m=300, stamp=None):
        return {
            "icao24": icao24,
            "callsign": icao24.upper(),
            "latitude": SENSOR[0] + north_km / 111.2,
            "longitude": SENSOR[1],
            "geo_altitude": alt_m,
            "velocity": 100,
            "true_track": 180,
            "last_contact": stamp or self.now,
            "source": "opensky",
        }

    def _pass(self, icao24):
        rows = [r for r in self.db.get_recent_events(50, "pass") if r["icao24"] == icao24]
        self.assertEqual(len(rows), 1, icao24)
        return rows[0]


class NoiseMatchingTests(_RecorderFixture):
    def test_pick_returns_the_peak_and_its_time(self):
        picked = proxy_server.pick_noise_peak(self.now - 20, band_s=45.0)
        self.assertEqual(picked, (60.0, self.now))
        self.assertIsNone(proxy_server.pick_noise_peak(self.now, band_s=45.0, exclude_ts=[self.now]))

    def test_slant_is_interpolated_between_samples(self):
        proxy_server.remember_track_point("abc", 100.0, 4.0)
        proxy_server.remember_track_point("abc", 200.0, 2.0)
        self.assertAlmostEqual(proxy_server.slant_at("abc", 150.0), 3.0)
        self.assertAlmostEqual(proxy_server.slant_at("abc", 260.0), 2.0)
        self.assertIsNone(proxy_server.slant_at("abc", 400.0))
        self.assertEqual(proxy_server.closest_track_point("abc"), (200.0, 2.0))

    def test_peak_goes_to_the_closer_aircraft_in_one_poll(self):
        proxy_server.process_aircraft_states(
            [self._state("far000", 3.0), self._state("near00", 1.0)], 8.0, 3200.0)
        near = self._pass("near00")
        far = self._pass("far000")
        self.assertEqual(near["spl_db"], 60.0)
        self.assertEqual(near["spl_peak_ts"], self.now)
        self.assertEqual(near["spl_max_db"], 65.0)   # LA_max of the sample at the peak
        self.assertIsNone(far["spl_db"])
        self.assertIsNone(far["spl_peak_ts"])

    def test_closer_aircraft_takes_the_peak_from_an_earlier_holder(self):
        proxy_server.process_aircraft_states([self._state("far000", 3.0, stamp=self.now - 30)], 8.0, 3200.0)
        self.assertEqual(self._pass("far000")["spl_db"], 60.0)
        proxy_server.process_aircraft_states([self._state("near00", 1.0)], 8.0, 3200.0)
        self.assertEqual(self._pass("near00")["spl_db"], 60.0)
        far = self._pass("far000")
        self.assertIsNone(far["spl_db"])
        closest = [r for r in self.db.get_recent_events(50, "closest_approach") if r["icao24"] == "far000"]
        self.assertEqual(len(closest), 1)
        self.assertIsNone(closest[0]["spl_db"])
        # The recalc worker finds nothing else for it: the only peak belongs to the closer aircraft.
        proxy_server.recalc_recent_passes_for_noise()
        self.assertIsNone(self._pass("far000")["spl_db"])

    def test_farther_aircraft_cannot_take_a_held_peak(self):
        proxy_server.process_aircraft_states([self._state("near00", 1.0, stamp=self.now - 30)], 8.0, 3200.0)
        proxy_server.process_aircraft_states([self._state("far000", 3.0)], 8.0, 3200.0)
        self.assertEqual(self._pass("near00")["spl_db"], 60.0)
        self.assertIsNone(self._pass("far000")["spl_db"])

    def test_two_peaks_two_aircraft(self):
        proxy_server.noise_data_history = _series(self.now, self.now - 40) + [
            {"timestamp": self.now + 40, "value": 55.0},
            {"timestamp": self.now + 50, "value": 40.0},
        ]
        proxy_server.noise_data_history.sort(key=lambda e: e["timestamp"])
        proxy_server.process_aircraft_states(
            [self._state("first0", 1.0, stamp=self.now - 40), self._state("second", 1.5, stamp=self.now + 40)],
            8.0, 3200.0)
        self.assertEqual(self._pass("first0")["spl_db"], 60.0)
        self.assertEqual(self._pass("second")["spl_db"], 55.0)

    def test_opensky_poll_floor_is_thirty_seconds(self):
        self.db.set_admin_setting("active_aircraft_feed", "aircraft")
        self.db.set_admin_setting("aircraft_poll_interval_seconds", "10")
        self.db.set_admin_setting("poll_quiet_start", "00:00")   # no quiet window in this test
        self.db.set_admin_setting("poll_quiet_end", "00:00")
        proxy_server.load_api_configs_from_database()
        try:
            aircraft, _noise = proxy_server._active_poll_intervals()
        finally:
            proxy_server.db = self._orig["db"]
            proxy_server.load_api_configs_from_database()
            proxy_server.db = self.db
        self.assertEqual(aircraft, 30)



class NoiseBufferTests(unittest.TestCase):
    def setUp(self):
        self._orig = proxy_server.noise_data_history
        proxy_server.noise_data_history = []

    def tearDown(self):
        proxy_server.noise_data_history = self._orig

    def test_overlapping_polls_keep_one_entry_per_timestamp(self):
        first = [{"timestamp": 100.0 + 10 * i, "value": 40.0} for i in range(10)]
        second = [{"timestamp": 150.0 + 10 * i, "value": 41.0} for i in range(10)]
        proxy_server.store_noise_data(first)
        proxy_server.store_noise_data(second)
        stamps = [e["timestamp"] for e in proxy_server.noise_data_history]
        self.assertEqual(stamps, [100.0 + 10 * i for i in range(15)])
        self.assertEqual(proxy_server.noise_data_history[5]["value"], 41.0)
        self.assertEqual(proxy_server.noise_data_history[4]["value"], 40.0)


class LateNoiseTests(unittest.TestCase):
    def setUp(self):
        self._orig = proxy_server.noise_data_history
        proxy_server.noise_data_history = [{"timestamp": 1000.0 + 10 * i, "value": 40.0 + i} for i in range(6)]

    def tearDown(self):
        proxy_server.noise_data_history = self._orig

    def test_no_sample_in_the_band_gives_nothing_instead_of_a_stale_sample(self):
        # Newest sample is at 1050; the aircraft is at 1135, 85 s later.
        self.assertIsNone(proxy_server.pick_noise_peak(1135.0, band_s=45.0))
        self.assertEqual(proxy_server.pick_noise_peak(1080.0, band_s=45.0), (45.0, 1050.0))


class TrackAndLiveTests(_RecorderFixture):
    """Flight path samples and the live snapshot, on the same fixture as the matching tests."""

    def test_recorder_stores_samples_and_the_live_snapshot(self):
        proxy_server.process_aircraft_states(
            [self._state("near00", 1.0, stamp=self.now - 30), self._state("far000", 9.0)], 8.0, 3200.0)
        proxy_server.process_aircraft_states([self._state("near00", 0.5)], 8.0, 3200.0)
        tracks = self.db.get_track_samples(["near00", "far000"], self.now - 600, self.now + 600)
        self.assertEqual([round(s["distance_km"], 1) for s in tracks["near00"]], [1.0, 0.5])
        self.assertNotIn("far000", tracks)  # outside the 8 km circle: no path
        snap = proxy_server.latest_aircraft_snapshot
        self.assertEqual([a["icao24"] for a in snap["aircraft"]], ["near00"])
        self.assertTrue(snap["aircraft"][0]["in_noise_range"])
        self.assertEqual(self.db.prune_track_samples(self.now + 1), 2)

    def test_live_and_tracks_endpoints(self):
        proxy_server.process_aircraft_states([self._state("near00", 1.0)], 8.0, 3200.0)
        client = proxy_server.app.test_client()
        live = client.get("/api/live_aircraft").get_json()
        self.assertEqual(live["aircraft"][0]["callsign"], "NEAR00")
        self.assertAlmostEqual(live["noise_slant_km"], 4.2)
        self.assertEqual(live["sensor"], list(proxy_server.public_sensor_position()))
        self.assertNotEqual(live["sensor"], list(SENSOR))      # the decoy, never the real spot
        tracks = client.get(f"/api/tracks?icao24=near00,zzz&from={int(self.now - 60)}&to={int(self.now + 60)}").get_json()
        self.assertEqual(len(tracks["tracks"]["near00"]), 1)
        zone = client.get("/api/detection_zone").get_json()
        self.assertAlmostEqual(zone["noise_slant_km"], 4.2)
        self.assertEqual(client.get("/api/tracks").get_json(), {"tracks": {}})


class _PhotoResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class AircraftPhotoTests(_RecorderFixture):
    def setUp(self):
        super().setUp()
        proxy_server._photo_cache.clear()
        proxy_server._photo_calls.clear()
        self.calls = []

        def fake_get(url, **kwargs):
            self.calls.append((url, kwargs.get('headers', {}).get('User-Agent')))
            if 'reg/PH-TFP' in url:
                return _PhotoResponse(200, {"photos": [{"thumbnail_large": {"src": "https://t.example/280.jpg"},
                                                        "link": "https://www.planespotters.net/photo/1", "photographer": "Bram"}]})
            return _PhotoResponse(200, {"photos": []})
        proxy_server.requests.get = fake_get

    def test_photo_by_registration_is_cached_and_credited(self):
        client = proxy_server.app.test_client()
        first = client.get("/api/aircraft_photo?reg=ph-tfp&hex=485a83").get_json()
        self.assertEqual(first["src"], "https://t.example/280.jpg")
        self.assertEqual(first["photographer"], "Bram")
        second = client.get("/api/aircraft_photo?reg=PH-TFP").get_json()
        self.assertEqual(second, first)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("+https://aircraftnoise.eu", self.calls[0][1])

    def test_no_photo_falls_back_to_hex_then_empty(self):
        client = proxy_server.app.test_client()
        self.assertEqual(client.get("/api/aircraft_photo?reg=N276GX&hex=abcdef").get_json(), {})
        self.assertEqual([u.rsplit('/pub/photos/', 1)[1] for u, _ in self.calls], ["reg/N276GX", "hex/abcdef"])
        client.get("/api/aircraft_photo?hex=abcdef")
        self.assertEqual(len(self.calls), 2)   # the miss is cached too
        self.assertEqual(client.get("/api/aircraft_photo?reg=x").get_json(), {})


class IdentityViewTests(unittest.TestCase):
    """Make, model and operator reach every view of a flight, whichever row learned them."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self.ts = 1_800_000_000.0   # 2027-01-15 08:00 UTC
        self.db.log_event({"ts": self.ts, "type": "closest_approach", "icao24": "4b1234", "callsign": "THY3EZ",
                           "distance_km": 2.4, "altitude_m": 1470, "spl_db": 51.5})
        self.db.log_event({"ts": self.ts + 20, "type": "pass", "icao24": "4b1234", "callsign": "THY3EZ",
                           "distance_km": 2.5, "altitude_m": 1500, "spl_db": 51.5,
                           "model": "A330", "operator": "THY", "registration": "TC-LOD"})

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_hour_view_fills_identity_from_the_other_row(self):
        rows = self.db.get_aircraft_events_by_date_hour("2027-01-15", 8, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "closest_approach")
        self.assertEqual(rows[0]["model"], "A330")
        self.assertEqual(rows[0]["operator"], "THY")

    def test_callsign_stats_carry_the_aircraft(self):
        stats = self.db.get_callsign_stats("thy3ez")
        self.assertEqual(stats["aircraft"]["registration"], "TC-LOD")
        self.assertEqual(stats["aircraft"]["model"], "A330")
        self.assertEqual(stats["aircraft"]["icao24"], "4b1234")

if __name__ == "__main__":
    unittest.main()
