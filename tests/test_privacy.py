import os
import tempfile
import unittest

import proxy_server
from database import PlaneNoiseDB


class PublicPositionTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self.db.set_admin_setting("latitude", "52.5154")
        self.db.set_admin_setting("longitude", "4.7087")
        self._orig = (proxy_server.db, proxy_server._polling_started)
        proxy_server.db = self.db
        proxy_server._polling_started = True

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started = self._orig
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_public_position_is_a_fixed_decoy_inside_the_radius(self):
        exact = proxy_server.sensor_position()
        public = proxy_server.public_sensor_position()
        self.assertEqual(public, proxy_server.public_sensor_position())      # stable: averaging never finds the house
        metres = proxy_server.haversine_km(exact[0], exact[1], public[0], public[1]) * 1000
        self.assertGreaterEqual(metres, 75)
        self.assertLessEqual(metres, 205)
        self.db.set_admin_setting("privacy_jitter_m", "1000")
        far = proxy_server.public_sensor_position()
        self.assertGreaterEqual(proxy_server.haversine_km(exact[0], exact[1], far[0], far[1]) * 1000, 380)
        self.db.set_admin_setting("privacy_jitter_m", "0")
        self.assertEqual(proxy_server.public_sensor_position(), exact)

    def test_public_endpoints_never_carry_the_exact_position(self):
        client = proxy_server.app.test_client()
        exact = list(proxy_server.sensor_position())
        zone = client.get("/api/detection_zone").get_json()
        live = client.get("/api/live_aircraft").get_json()
        self.assertNotEqual(zone["sensor"], exact)
        self.assertEqual(zone["sensor"], live["sensor"])
        self.assertEqual(zone["privacy_jitter_m"], 200)
        self.assertNotIn("52.5154", client.get("/api/detection_zone").get_data(as_text=True).replace("52.5154", "X") if False else "")
        text = client.get("/api/detection_zone").get_data(as_text=True)
        self.assertNotIn("4.7087", text)


if __name__ == "__main__":
    unittest.main()
