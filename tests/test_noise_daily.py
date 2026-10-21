import os
import tempfile
import unittest
from datetime import datetime, timedelta

import pytz

import proxy_server
from database import PlaneNoiseDB


class NoiseDailyTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self.db.set_admin_setting("site_timezone", "Europe/Amsterdam")
        self._orig = (proxy_server.db, proxy_server._polling_started)
        proxy_server.db = self.db
        proxy_server._polling_started = True
        self.zone = pytz.timezone("Europe/Amsterdam")

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started = self._orig
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def _pass(self, hours_ago_local_hour, spl, callsign, day_offset=0):
        now = datetime.now(self.zone)
        when = self.zone.localize(datetime(now.year, now.month, now.day, hours_ago_local_hour, 30)) - timedelta(days=day_offset)
        self.db.log_event({"ts": when.timestamp(), "type": "pass", "icao24": callsign.lower(), "callsign": callsign,
                           "distance_km": 2.0, "altitude_m": 1500, "spl_db": spl, "spl_max_db": None if spl is None else spl + 4,
                           "in_zone": True})   # every seeded pass crossed the zone; unmeasured ones still count as passes

    def test_bands_night_and_loudest_per_local_day(self):
        self._pass(1, 52.0, "NIGHT1")          # 01:30 local: night, band 50
        self._pass(14, 57.5, "DAY55")          # band 55
        self._pass(15, 61.0, "DAY60")          # band 60, loudest today
        self._pass(16, 48.0, "QUIET")          # below every band
        self._pass(17, None, "NODB")           # no decibel
        self._pass(12, 63.2, "YESTERDAY", day_offset=1)
        j = proxy_server.noise_daily_summary(days=3)
        today = j["today"]
        self.assertEqual((today["passes"], today["with_db"]), (5, 4))
        self.assertEqual((today["band_50"], today["band_55"], today["band_60"], today["night_50"]), (1, 1, 1, 1))
        self.assertEqual(today["loudest"]["callsign"], "DAY60")
        self.assertEqual(today["loudest"]["spl_max_db"], 65.0)
        self.assertEqual(j["loudest"]["callsign"], "YESTERDAY")
        self.assertEqual(len(j["series"]), 3)
        self.assertEqual(j["series"][-2]["band_60"], 1)

    def test_endpoint_shape(self):
        client = proxy_server.app.test_client()
        j = client.get("/api/stats/noise/daily?days=2").get_json()
        self.assertEqual(j["bands"], [50.0, 55.0, 60.0])
        self.assertEqual(len(j["series"]), 2)
        self.assertIsNone(j["loudest"])


if __name__ == "__main__":
    unittest.main()
