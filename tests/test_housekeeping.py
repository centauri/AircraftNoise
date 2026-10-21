import os
import tempfile
import unittest
from datetime import datetime

import pytz

import proxy_server
from database import PlaneNoiseDB


class HousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "data", "plane_noise.db")
        os.makedirs(os.path.dirname(self.path))
        self.db = PlaneNoiseDB(self.path)
        self.db.set_admin_setting("site_timezone", "Europe/Amsterdam")
        self._orig = (proxy_server.db, proxy_server._polling_started, dict(proxy_server._last_backup))
        proxy_server.db = self.db
        proxy_server._polling_started = True

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started, saved = self._orig
        proxy_server._last_backup.clear(); proxy_server._last_backup.update(saved)
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- quiet hours
    def test_schedule_estimate_and_interval_by_time(self):
        self.db.set_admin_setting("poll_day_interval_s", "20")
        self.db.set_admin_setting("poll_quiet_start", "01:00")
        self.db.set_admin_setting("poll_quiet_end", "05:00")
        self.db.set_admin_setting("poll_quiet_interval_s", "60")
        sch = proxy_server.opensky_schedule()
        self.assertEqual((sch["day_interval_s"], sch["quiet_interval_s"]), (20, 60))
        self.assertEqual(sch["calls_per_day"], 20 * 180 + 4 * 60)
        self.assertTrue(sch["within_allowance"])
        zone = pytz.timezone("Europe/Amsterdam")
        self.assertTrue(proxy_server._in_quiet_hours(sch, zone.localize(datetime(2026, 10, 20, 3, 0))))
        self.assertFalse(proxy_server._in_quiet_hours(sch, zone.localize(datetime(2026, 10, 20, 14, 0))))
        # Quiet window across midnight
        self.db.set_admin_setting("poll_quiet_start", "23:30")
        sch = proxy_server.opensky_schedule()
        self.assertTrue(proxy_server._in_quiet_hours(sch, zone.localize(datetime(2026, 10, 20, 0, 10))))
        self.assertFalse(proxy_server._in_quiet_hours(sch, zone.localize(datetime(2026, 10, 20, 6, 0))))

    def test_over_budget_falls_back_to_the_flat_floor(self):
        self.db.set_admin_setting("poll_day_interval_s", "20")
        self.db.set_admin_setting("poll_quiet_interval_s", "20")   # 4,320 a day
        self.assertFalse(proxy_server.opensky_schedule()["within_allowance"])
        self.assertEqual(proxy_server.opensky_interval_now(), proxy_server.OPENSKY_MIN_POLL_INTERVAL)
        self.assertEqual(proxy_server.opensky_schedule({"poll_day_interval_s": "5"})["day_interval_s"], proxy_server.OPENSKY_DAY_FLOOR)

    # ---- backups
    def test_backup_lands_in_the_data_folder_and_rotates(self):
        self.db.set_admin_setting("backup_keep", "2")
        paths = [proxy_server.backup_database() for _ in range(3)]
        folder = os.path.join(self.dir, "data", "backups")
        self.assertEqual(os.path.dirname(paths[0]), folder)
        kept = sorted(n for n in os.listdir(folder) if n.endswith(".db"))
        self.assertLessEqual(len(kept), 2)
        self.assertIsNotNone(proxy_server._last_backup["ts"])
        import sqlite3
        copy = sqlite3.connect(paths[-1])
        self.assertEqual(copy.execute("select value from admin_settings where key='backup_keep'").fetchone()[0], "2")
        copy.close()

    # ---- retention
    def test_event_retention_floor_and_prune(self):
        import time
        self.db.set_admin_setting("retention_events_days", "5")   # below the 30-day floor
        self.assertEqual(proxy_server.retention_settings()["events_days"], 30)
        self.db.set_admin_setting("retention_events_days", "0")
        self.assertEqual(proxy_server.retention_settings()["events_days"], 0)
        old = time.time() - 40 * 86400
        self.db.log_event({"ts": old, "type": "pass", "icao24": "old001", "distance_km": 2.0})
        self.db.log_event({"ts": time.time(), "type": "pass", "icao24": "new001", "distance_km": 2.0})
        self.assertEqual(self.db.prune_events(time.time() - 30 * 86400), 1)
        self.assertEqual([r["icao24"] for r in self.db.get_recent_events(10, "pass")], ["new001"])

    # ---- callsign history
    def test_callsign_passes_endpoint(self):
        import time
        for i in range(3):
            self.db.log_event({"ts": time.time() - i * 3600, "type": "pass", "icao24": f"ab{i:04d}", "callsign": "KLM123",
                               "distance_km": 1.5, "altitude_m": 900, "spl_db": 50 + i, "spl_max_db": 55 + i, "registration": "PH-BXA"})
        client = proxy_server.app.test_client()
        j = client.get("/api/callsigns/klm123/passes?limit=2").get_json()
        self.assertEqual(j["callsign"], "KLM123")
        self.assertEqual([p["spl_db"] for p in j["passes"]], [50.0, 51.0])
        self.assertEqual(j["passes"][0]["spl_max_db"], 55.0)


if __name__ == "__main__":
    unittest.main()
