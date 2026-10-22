import os
import tempfile
import unittest

import proxy_server
from database import PlaneNoiseDB


class SetupWalkthroughTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self._orig = (proxy_server.db, proxy_server._polling_started)
        proxy_server.db = self.db
        proxy_server._polling_started = True
        self.client = proxy_server.app.test_client()

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started = self._orig
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def _create_account(self, **overrides):
        body = {"name": "Jane Example", "email": "paul@example.com", "password": "correct horse battery", "confirm": "correct horse battery"}
        body.update(overrides)
        return self.client.post("/setup/account", json=body)

    def test_fresh_install_goes_to_the_walkthrough(self):
        self.assertEqual(proxy_server.setup_state(), "account")
        for path in ("/admin", "/admin/login", "/admin/dashboard"):
            r = self.client.get(path)
            self.assertEqual((path, r.status_code), (path, 302))
            self.assertTrue(r.headers["Location"].endswith("/setup"))
        self.assertEqual(self.client.get("/setup").status_code, 200)
        self.assertEqual(self.client.get("/setup/state").get_json()["state"], "account")

    def test_account_step_validates_and_signs_in(self):
        self.assertEqual(self._create_account(password="short", confirm="short").status_code, 400)
        self.assertEqual(self._create_account(confirm="something else").status_code, 400)
        self.assertEqual(self._create_account(email="nope").status_code, 400)
        self.assertEqual(self.db.count_users(), 0)
        r = self._create_account()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.count_users(), 1)
        self.assertEqual(self.client.get("/auth/me").get_json()["user"]["email"], "paul@example.com")
        self.assertEqual(proxy_server.setup_state(), "station")
        # Once an account exists the step is closed for good.
        self.assertEqual(self._create_account(email="other@example.com").status_code, 403)
        self.assertEqual(self.db.count_users(), 1)

    def test_admin_pages_wait_until_the_walkthrough_is_done_or_skipped(self):
        self._create_account()
        r = self.client.get("/admin/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/setup"))
        # Saves and data calls still work while the walkthrough is owed.
        self.assertEqual(self.client.post("/api/admin/settings", json={"site_place_name": "Castricum"}).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/settings").status_code, 200)
        opts = self.client.get("/api/setup/options").get_json()
        self.assertIn("nl", opts["countries"])
        self.assertIn("Europe/Amsterdam", opts["timezones"])
        self.assertEqual({f["id"] for f in opts["feeds"] if f["kind"] == "aircraft"}, {"aircraft", "aircraft_hub", "aircraft_airplanes_live", "aircraft_adsb_fi", "aircraft_adsb_lol", "aircraft_flightaware"})
        self.assertEqual(self.client.post("/api/setup/advance", json={"to": "skipped"}).get_json()["state"], "skipped")
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 200)
        self.assertEqual(self.client.post("/api/setup/advance", json={"to": "done"}).get_json()["state"], "done")
        r = self.client.get("/setup")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/admin/dashboard"))

    def test_existing_install_without_the_flag_counts_as_done(self):
        self.db.create_user("old@example.com", proxy_server.hash_password("old password 123"), "Old", "Admin", is_admin=True)
        self.assertEqual(proxy_server.setup_state(), "done")
        r = self.client.get("/admin/login")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post("/setup/account", json={}).status_code, 403)

    def test_walkthrough_needs_a_session_after_the_account_step(self):
        self._create_account()
        self.client.post("/auth/logout")
        r = self.client.get("/setup")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/admin/login"))
        self.assertEqual(self.client.post("/api/setup/advance", json={"to": "done"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
