"""Browser checks of the public pages and the first-run walkthrough.

They need Playwright and a Chromium build, so they run only on demand:

    pip install playwright && playwright install chromium
    PLANENOISE_BROWSER_TESTS=1 python3 -m unittest tests.browser.test_pages

The app runs in a thread on a free port with a seeded temporary database and
the feed pollers off. Nothing reaches the network except OpenStreetMap tiles.
"""
import os
import random
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

RUN = os.getenv("PLANENOISE_BROWSER_TESTS") == "1"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(RUN, "set PLANENOISE_BROWSER_TESTS=1 to run the browser checks")
class PublicPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pytz
        from werkzeug.serving import make_server
        import proxy_server
        from database import PlaneNoiseDB
        from playwright.sync_api import sync_playwright

        cls.dir = tempfile.mkdtemp()
        cls.db = PlaneNoiseDB(os.path.join(cls.dir, "plane_noise.db"))
        cls.db.set_admin_setting("site_timezone", "Europe/Amsterdam")
        zone = pytz.timezone("Europe/Amsterdam")
        random.seed(1)
        now = datetime.now(zone)
        for back in range(3):
            for k in range(12):
                when = now - timedelta(days=back, minutes=k * 37)
                spl = random.choice([None, 46.0, 52.0, 57.5, 61.0])
                cls.db.log_event({"ts": when.timestamp(), "type": "pass", "icao24": f"{random.randint(0, 999999):06x}",
                                  "callsign": random.choice(["KLM123", "TRA32A", "EZY59XP"]), "distance_km": round(random.uniform(0.5, 6), 2),
                                  "altitude_m": random.randint(600, 3000), "spl_db": spl, "spl_max_db": None if spl is None else spl + 4,
                                  "spl_peak_ts": when.timestamp() + 20 if spl else None, "model": "A320",
                                  "latitude": 52.52 + random.uniform(-0.04, 0.04), "longitude": 4.71 + random.uniform(-0.05, 0.05)})
        cls._orig = (proxy_server.db, proxy_server._polling_started)
        proxy_server.db = cls.db
        proxy_server._polling_started = True
        cls.port = _free_port()
        cls.server = make_server("127.0.0.1", cls.port, proxy_server.app, threaded=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        import proxy_server
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        proxy_server.db, proxy_server._polling_started = cls._orig

    def _page(self):
        page = self.browser.new_page(viewport={"width": 1300, "height": 900}, bypass_csp=True)
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and "X-Frame-Options" not in m.text and "tile.openstreetmap" not in m.text else None)
        return page

    def test_public_page_table_map_and_summary(self):
        page = self._page()
        page.goto(f"{self.base}/", wait_until="load", timeout=60000)
        page.wait_for_timeout(6000)
        self.assertGreater(page.locator("#eventsTableBody tr").count(), 5)
        self.assertGreater(page.evaluate("Object.values(map._layers).filter(l => l instanceof L.CircleMarker).length"), 3)
        self.assertEqual(page.locator(".plane-map-legend").count(), 1)
        self.assertEqual(page.locator("#noiseSummaryCard canvas").count(), 1)
        self.assertNotEqual(page.locator("#todayLoudest").inner_text().strip(), "")
        self.assertEqual(self.errors, [])
        page.close()

    def test_dutch_page_renders(self):
        page = self._page()
        page.goto(f"{self.base}/nl", wait_until="load", timeout=60000)
        page.wait_for_timeout(6000)
        self.assertGreater(page.locator("#eventsTableBody tr").count(), 5)
        self.assertEqual(self.errors, [])
        page.close()

    def test_walkthrough_opens_on_a_fresh_install(self):
        import proxy_server
        from database import PlaneNoiseDB
        fresh = PlaneNoiseDB(os.path.join(self.dir, "fresh.db"))
        saved = proxy_server.db
        proxy_server.db = fresh
        try:
            page = self._page()
            page.goto(f"{self.base}/admin/login", wait_until="load", timeout=60000)
            page.wait_for_timeout(1000)
            self.assertTrue(page.url.endswith("/setup"))
            self.assertEqual(page.locator(".step.active").get_attribute("data-step"), "account")
            page.close()
        finally:
            proxy_server.db = saved


if __name__ == "__main__":
    unittest.main()
