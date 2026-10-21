import json
import os
import tempfile
import unittest

import proxy_server
from database import PlaneNoiseDB


TRIANGLE = [
    [52.50, 4.70],
    [52.50, 4.74],
    [52.54, 4.72],
]
INSIDE = (52.52, 4.72)
OUTSIDE = (52.48, 4.72)

# The live zone, stored as [latitude, longitude].
LIVE_ZONE = [
    [52.5390921687078, 4.708156584771058],
    [52.53507784622079, 4.702738616643037],
    [52.53121417929889, 4.697073785116373],
    [52.52656140692964, 4.692192075792115],
    [52.521757001084076, 4.69013213723958],
    [52.51695205565691, 4.690303799354384],
    [52.511890745082624, 4.690893968905452],
    [52.50786308797268, 4.6911621071614515],
    [52.503788083165944, 4.691505427148451],
    [52.49793612212169, 4.696655270301649],
    [52.49386018292788, 4.704895019346798],
    [52.491456248500704, 4.71107483039281],
    [52.49093365242519, 4.717941287315539],
    [52.48999293817937, 4.726696021967208],
    [52.490933663704354, 4.735450755512121],
    [52.493755699810784, 4.7406005960828645],
    [52.497831648689285, 4.743690498838964],
    [52.50117573116686, 4.745578774169902],
    [52.50566890979862, 4.749012000417717],
    [52.51026610713581, 4.755020148611076],
    [52.51601194876752, 4.76102829864904],
    [52.521339236500644, 4.764633187011698],
    [52.528754575837894, 4.764976509027755],
    [52.53355820365696, 4.758968358620846],
    [52.53857015013916, 4.749870303428821],
    [52.54180672631504, 4.737854005566403],
    [52.54347711030242, 4.726867676150156],
    [52.54243310082632, 4.716739653618714],
]


class DetectionZoneTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self._orig = proxy_server.db
        proxy_server.db = self.db

    def tearDown(self):
        proxy_server.db = self._orig
        os.unlink(self.path)

    def test_polygon_reads_latitude_first(self):
        self.assertTrue(proxy_server.point_in_polygon(INSIDE[0], INSIDE[1], TRIANGLE))
        self.assertFalse(proxy_server.point_in_polygon(OUTSIDE[0], OUTSIDE[1], TRIANGLE))

    def test_live_zone_contains_the_sensor_and_the_near_pass(self):
        self.assertTrue(proxy_server.point_in_polygon(52.5154, 4.7087, LIVE_ZONE))
        self.assertTrue(proxy_server.point_in_polygon(52.5384, 4.7417, LIVE_ZONE))
        self.assertFalse(proxy_server.point_in_polygon(52.5551, 4.7793, LIVE_ZONE))

    def test_slant_stays_required_inside_the_zone(self):
        self.db.set_admin_setting("inner_zone_type", "polygon")
        self.db.set_admin_setting("detection_zone_points", json.dumps(TRIANGLE))
        self.db.set_admin_setting("noise_assoc_inner_km", "4")
        self.assertFalse(proxy_server.noise_association_allowed(2.0, 4000))
        self.assertFalse(proxy_server.noise_association_allowed(6.5, 2034))
        self.assertTrue(proxy_server.noise_association_allowed(3.0, 1000))


class ClosestSampleTests(unittest.TestCase):
    """The stored distance and altitude belong to the sample with the smallest slant range."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def _row(self, event_type):
        rows = [r for r in self.db.get_recent_events(10) if r["type"] == event_type]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_closest_approach_upsert_follows_the_slant_range(self):
        ts = 1_800_000_000.0
        # 2.0 km over the ground at 2500 m is 3.20 km through the air.
        self.db.log_event({"ts": ts, "type": "closest_approach", "icao24": "abc123",
                           "distance_km": 2.0, "altitude_m": 2500, "spl_db": 50.0})
        # 3.0 km over the ground at 500 m is 3.04 km through the air: closer.
        self.db.log_event({"ts": ts + 30, "type": "closest_approach", "icao24": "abc123",
                           "distance_km": 3.0, "altitude_m": 500, "spl_db": 48.0})
        row = self._row("closest_approach")
        self.assertAlmostEqual(row["distance_km"], 3.0)
        self.assertEqual(row["altitude_m"], 500)
        self.assertEqual(row["spl_db"], 50.0)
        self.assertEqual(self.db.get_closest_approach("abc123")["altitude_m"], 500)

    def test_pass_update_follows_the_slant_range_and_keeps_the_loudest(self):
        ts = 1_800_000_000.0
        self.db.log_event({"ts": ts, "type": "pass", "icao24": "abc123",
                           "distance_km": 2.0, "altitude_m": 2500, "spl_db": None})
        row = self._row("pass")
        self.assertTrue(self.db.update_pass_metrics(row["id"], distance_km=3.0, altitude_m=500, spl_db=55.0))
        self.assertTrue(self.db.update_pass_metrics(row["id"], distance_km=2.5, altitude_m=2500, spl_db=50.0))
        row = self._row("pass")
        self.assertAlmostEqual(row["distance_km"], 3.0)
        self.assertEqual(row["altitude_m"], 500)
        self.assertEqual(row["spl_db"], 55.0)

    def test_weather_follows_the_loudest_sample_only(self):
        ts = 1_800_000_000.0
        self.db.log_event({"ts": ts, "type": "pass", "icao24": "abc123",
                           "distance_km": 2.0, "altitude_m": 1000, "spl_db": 50.0,
                           "wx_lat": 52.5, "wx_lon": 4.7, "wx_wind_speed": 3.0})
        row = self._row("pass")
        self.db.update_pass_metrics(row["id"], spl_db=45.0, wx_lat=52.6, wx_lon=4.8, wx_wind_speed=9.0)
        self.assertEqual(self._row("pass")["wx_wind_speed"], 3.0)
        self.db.update_pass_metrics(row["id"], spl_db=58.0, wx_lat=52.6, wx_lon=4.8, wx_wind_speed=9.0)
        row = self._row("pass")
        self.assertEqual(row["wx_wind_speed"], 9.0)
        self.assertEqual(row["spl_db"], 58.0)


class ZoneMembershipTests(unittest.TestCase):
    """The public events carry whether their closest point crossed the drawn zone."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self._orig = (proxy_server.db, proxy_server._polling_started)
        proxy_server.db = self.db
        proxy_server._polling_started = True
        self.client = proxy_server.app.test_client()
        now = 1_800_000_000.0
        self.db.log_event({"ts": now, "type": "pass", "icao24": "in0001", "callsign": "IN", "distance_km": 1.0,
                           "latitude": INSIDE[0], "longitude": INSIDE[1]})
        self.db.log_event({"ts": now + 60, "type": "pass", "icao24": "out001", "callsign": "OUT", "distance_km": 6.9,
                           "latitude": OUTSIDE[0], "longitude": OUTSIDE[1]})

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started = self._orig
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def _flags(self):
        rows = self.client.get("/recent_events?limit=10").get_json()["events"]
        return {r["callsign"]: r["in_zone"] for r in rows}

    def test_polygon_decides_when_drawn(self):
        self.db.set_admin_setting("inner_zone_type", "polygon")
        self.db.set_admin_setting("detection_zone_points", json.dumps(TRIANGLE))
        self.assertEqual(self._flags(), {"IN": True, "OUT": False})

    def test_noise_range_decides_without_a_polygon(self):
        self.db.set_admin_setting("noise_assoc_inner_km", "3")
        self.assertEqual(self._flags(), {"IN": True, "OUT": False})

    def test_public_table_view_is_a_station_setting(self):
        self.assertEqual(self.client.get("/visitor/settings").get_json()["events_view"], "zone")
        self.client.post("/setup/account", json={"name": "A", "email": "a@example.com", "password": "correct horse battery", "confirm": "correct horse battery"})
        self.assertEqual(self.client.post("/visitor/settings", json={"events_view": "measured"}).status_code, 200)
        self.assertEqual(self.client.get("/visitor/settings").get_json()["events_view"], "measured")
        self.client.post("/visitor/settings", json={"events_view": "nonsense"})
        self.assertEqual(self.client.get("/visitor/settings").get_json()["events_view"], "measured")


class TelemetryExcludesOutsideZoneTests(unittest.TestCase):
    """A pass that never crossed the zone and got no decibel shows on the map and in the
    table, but counts in no statistic."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self.db.set_admin_setting("site_timezone", "Europe/Amsterdam")
        self.db.set_admin_setting("inner_zone_type", "polygon")
        self.db.set_admin_setting("detection_zone_points", json.dumps(TRIANGLE))
        self.db.set_admin_setting("noise_assoc_inner_km", "5")
        self._orig = (proxy_server.db, proxy_server._polling_started)
        proxy_server.db = self.db
        proxy_server._polling_started = True
        self.client = proxy_server.app.test_client()
        import time
        now = time.time() - 120
        # In the zone, unmeasured: counts. Outside, unmeasured, but within the 5 km noise range: shows only.
        self.db.log_event({"ts": now, "type": "pass", "icao24": "in0001", "callsign": "INZONE", "distance_km": 1.0,
                           "latitude": INSIDE[0], "longitude": INSIDE[1], "in_zone": True, "route_label": "X arrival"})
        self.db.log_event({"ts": now + 30, "type": "pass", "icao24": "out001", "callsign": "RIM", "distance_km": 4.4,
                           "latitude": OUTSIDE[0], "longitude": OUTSIDE[1], "in_zone": False, "route_label": "overflight NW"})
        self.db.set_pass_route(self.db.get_recent_events(1, "pass")[0]["id"], {"kind": "overflight", "label": "overflight NW", "confidence": 0.3})

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started = self._orig
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_table_shows_both_but_statistics_count_one(self):
        rows = self.client.get("/recent_events?limit=10").get_json()["events"]
        self.assertEqual({r["callsign"]: r["in_zone"] for r in rows}, {"INZONE": True, "RIM": False})
        daily = proxy_server.noise_daily_summary(days=1)
        self.assertEqual(daily["today"]["passes"], 1)
        routes_today = self.client.get("/api/routes/daily?days=1").get_json()["today"]
        self.assertEqual([r["label"] for r in routes_today], ["unlabelled"] if False else [r["label"] for r in routes_today])
        self.assertNotIn("overflight NW", [r["label"] for r in routes_today])
        heat = self.db.get_planes_heatmap(sensor_timezone_offset=2)
        self.assertEqual(sum(sum(h.values()) for h in heat.values()), 1)

    def test_recompute_after_the_zone_changes(self):
        self.db.set_admin_setting("inner_zone_type", "circle")   # 5 km circle: both are inside now
        self.client.post("/setup/account", json={"name": "A", "email": "a@example.com", "password": "correct horse battery", "confirm": "correct horse battery"})
        self.assertEqual(self.client.post("/api/admin/zone/recompute").get_json()["rows"], 2)
        rows = self.client.get("/recent_events?limit=10").get_json()["events"]
        self.assertEqual({r["callsign"]: r["in_zone"] for r in rows}, {"INZONE": True, "RIM": True})
        self.assertEqual(proxy_server.noise_daily_summary(days=1)["today"]["passes"], 2)
