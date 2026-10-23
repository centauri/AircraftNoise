"""What the station exposes without an admin session: the public pages and the
read-only endpoints they use, nothing that changes settings, writes events or
relays a feed with the station's credentials."""
import os
import tempfile
import unittest


class ExposureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["PN_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "exposure.db")
        import proxy_server
        import guard
        from database import PlaneNoiseDB
        cls.server = proxy_server
        cls._orig_db = proxy_server.db
        proxy_server.db = PlaneNoiseDB(os.environ["PN_DB_PATH"])
        proxy_server._polling_started = True
        proxy_server.app.config["TESTING"] = True
        proxy_server.scraper_guard = guard.Guard(store=None, resolve=lambda ip: False)
        cls.client = proxy_server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.db = cls._orig_db

    def test_admin_actions_and_feed_relays_need_a_session(self):
        closed = {
            "GET": ["/api/admin/get_detection_zone", "/opensky?bbox=1,2,3,4", "/fetch_csv?url=https://x", "/grafana_csv?csv_url=x",
                    "/aircraft_flights?icao24=4cad96", "/aircraft_meta?icao24=4cad96", "/flight_track?icao24=4cad96",
                    "/test_destination?icao24=4cad96", "/sensor_api?id=1", "/health", "/legacy", "/admin/dashboard", "/admin/stats", "/api/admin/settings"],
            "POST": ["/api/admin/save_detection_zone", "/api/admin/reset_detection_zone", "/append_log", "/log_match", "/grafana_query"],
        }
        for method, paths in closed.items():
            for path in paths:
                r = self.client.open(path, method=method, json={} if method == "POST" else None)
                self.assertIn(r.status_code, (401, 302, 303, 404), f"{method} {path} answered {r.status_code} without a session")
                if r.status_code in (302, 303):  # to the login, or to the setup on a fresh install
                    self.assertTrue(any(x in r.headers.get("Location", "") for x in ("/admin/login", "/setup")), path)

    def test_public_pages_and_their_endpoints_stay_open(self):
        for path in ["/", "/community", "/types.html", "/api/health", "/visitor/settings", "/api/detection_zone",
                     "/recent_events?limit=5", "/api/noise/recent", "/api/live_aircraft"]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
        r = self.client.get("/setup")  # open on a fresh install, a redirect to the login once an account exists
        self.assertIn(r.status_code, (200, 302, 303))

    # Every route the app registers, called without a session. A new route is
    # closed unless it is added to this list on purpose.
    PUBLIC = {
        "/", "/public", "/community", "/types.html", "/callsign.html", "/setup", "/setup/state", "/setup/account",
        "/sitemap.xml", "/robots.txt", "/api/health", "/visitor/settings", "/visitor/aircraft", "/visitor/theme", "/api/aircraft",
        "/api/detection_zone", "/recent_events", "/api/events", "/api/noise/recent", "/api/live_aircraft", "/api/tracks",
        "/api/routes/daily", "/api/stats/aircraft_types", "/api/stats/noise/daily", "/api/stats/planes/details",
        "/api/stats/planes/heatmap", "/api/aircraft_photo", "/api/complaint_target", "/api/community/monitors",
        "/api/callsigns/<callsign>/passes", "/api/callsigns/<callsign>/stats", "/api/event/<int:event_id>",
        "/nl/", "/nl/public", "/nl/community", "/auth/login", "/auth/logout", "/auth/me", "/auth/password-reset",
        "/static/<path:filename>", "/trap/<path:rest>", "/admin", "/admin/login", "/auth/reset/<token>",
    }

    def test_every_other_route_is_closed_without_a_session(self):
        import re
        app = self.server.app
        fresh = app.test_client()
        open_routes = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static" or rule.rule in self.PUBLIC:
                continue
            path = re.sub(r"<int:[^>]+>", "1", rule.rule)
            path = re.sub(r"<[^>]+>", "x", path)
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                r = fresh.open(path, method=method, json={} if method != "GET" else None)
                location = r.headers.get("Location", "")
                closed = r.status_code in (401, 403, 405) or (
                    r.status_code in (301, 302, 303) and ("/admin/login" in location or "/setup" in location))
                if not closed:
                    open_routes.append(f"{method} {rule.rule} -> {r.status_code}")
        self.assertEqual(open_routes, [], "routes answering without a session that are not declared public")

    def test_static_files_cannot_escape_their_folder(self):
        for path in ("/static/../proxy_server.py", "/static/..%2fproxy_server.py", "/static/../../etc/passwd"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_login_is_throttled_after_repeated_failures(self):
        p = self.server
        p._login_failures.clear()
        fresh = p.app.test_client()
        fresh.post("/setup/account", json={"name": "A", "email": "a@example.com", "password": "correct horse battery", "confirm": "correct horse battery"})
        fresh = p.app.test_client()
        codes = [fresh.post("/auth/login", json={"email": "a@example.com", "password": "wrong"}).status_code for _ in range(p.LOGIN_FAILURES)]
        self.assertEqual(codes, [401] * p.LOGIN_FAILURES)
        r = fresh.post("/auth/login", json={"email": "a@example.com", "password": "correct horse battery"})
        self.assertEqual(r.status_code, 429, "after the limit even the right password waits")
        self.assertTrue(int(r.headers.get("Retry-After", "0")) > 0)
        # Another address against the same account is blocked too (per account), a fresh pair is not.
        other = p.app.test_client()
        self.assertEqual(other.post("/auth/login", json={"email": "a@example.com", "password": "wrong"}, environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code, 429)
        self.assertEqual(other.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong"}, environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code, 401)
        p._login_failures.clear()
        self.assertEqual(fresh.post("/auth/login", json={"email": "a@example.com", "password": "correct horse battery"}).status_code, 200)

    def test_forgotten_password_by_mail_link_or_by_shell_script(self):
        import subprocess, sys
        p = self.server
        p._login_failures.clear()
        fresh = p.app.test_client()
        fresh.post("/setup/account", json={"name": "A", "email": "a@example.com", "password": "correct horse battery", "confirm": "correct horse battery"})
        try:
            self.server.db.create_user("r@example.com", p.hash_password("original password 1"), "Reset", "Tester", is_admin=True)
        except Exception:
            pass  # already there from an earlier run of this class
        anon = p.app.test_client()
        # No mail relay: the answer names the shell script.
        orig = p._mail_configured
        p._mail_configured = lambda: False
        r = anon.post("/auth/password-reset", json={"email": "r@example.com"})
        self.assertEqual(r.status_code, 501); self.assertIn("reset_password.py", r.get_json()["message"])
        # With a relay: a link by mail, good for an hour, once.
        import notifications
        sent = []
        orig_send = notifications.send_email
        notifications.send_email = lambda to, subject, body: sent.append((to, body)) or (True, "ok")
        p._mail_configured = lambda: True
        try:
            r = anon.post("/auth/password-reset", json={"email": "r@example.com"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(anon.post("/auth/password-reset", json={"email": "nobody@example.com"}).status_code, 200, "the same answer for an unknown address")
            self.assertEqual(len(sent), 1)
            link = [w for w in sent[0][1].split() if "/auth/reset/" in w][0]
            path = link[link.index("/auth/reset/"):]
            self.assertEqual(anon.get(path).status_code, 200)
            self.assertEqual(anon.post(path, data={"password": "short", "confirm": "short"}).status_code, 400)
            r = anon.post(path, data={"password": "a brand new password", "confirm": "a brand new password"})
            self.assertEqual(r.status_code, 302); self.assertIn("reset=1", r.headers["Location"])
            self.assertEqual(anon.get(path).status_code, 410, "a used link is dead")
            p._login_failures.clear()
            self.assertEqual(anon.post("/auth/login", json={"email": "r@example.com", "password": "a brand new password"}).status_code, 200)
        finally:
            notifications.send_email = orig_send
            p._mail_configured = orig
        # The shell script sets a password on the same database.
        env = dict(os.environ, PN_DB_PATH=os.environ["PN_DB_PATH"])
        out = subprocess.run([sys.executable, "scripts/reset_password.py", "r@example.com", "--password", "set from the shell"],
                             capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(out.returncode, 0, out.stderr[-300:])
        p._login_failures.clear()
        self.assertEqual(p.app.test_client().post("/auth/login", json={"email": "r@example.com", "password": "set from the shell"}).status_code, 200)

    def test_public_zone_never_gives_the_exact_sensor(self):
        db = self.server.db
        db.set_admin_setting("sensor_lat", "51.9000")
        db.set_admin_setting("sensor_lon", "4.4000")
        db.set_admin_setting("privacy_jitter_m", "200")
        zone = self.client.get("/api/detection_zone").get_json()
        lat, lon = zone["sensor"]
        self.assertNotEqual((round(lat, 4), round(lon, 4)), (51.9, 4.4), "the public map gets a decoy point, not the microphone")
        self.assertLess(abs(lat - 51.9), 0.01)


if __name__ == "__main__":
    unittest.main()


class MailServerSettingsTests(unittest.TestCase):
    """The mail server comes from the Alerts page, password as a secret, with
    the MAIL_* environment as the fallback; alerts and the password reset use it."""

    def setUp(self):
        os.environ["PN_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "mail.db")
        import proxy_server, notifications
        from database import PlaneNoiseDB
        self.p, self.n = proxy_server, notifications
        self._orig_db = proxy_server.db
        proxy_server.db = PlaneNoiseDB(os.environ["PN_DB_PATH"])
        self._orig_getter = notifications.settings_getter
        notifications.settings_getter = proxy_server.db.get_admin_setting
        proxy_server._polling_started = True
        proxy_server.app.config["TESTING"] = True
        self.client = proxy_server.app.test_client()
        self.client.post("/setup/account", json={"name": "A", "email": "a@example.com", "password": "correct horse battery", "confirm": "correct horse battery"})
        for k in ("MAIL_SERVER", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_DEFAULT_SENDER"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.p.db = self._orig_db
        self.n.settings_getter = self._orig_getter

    def test_settings_page_values_drive_the_mail_and_the_password_is_a_secret(self):
        n, c = self.n, self.client
        self.assertFalse(n.mail_configured(), "nothing set: no mail")
        r = c.post("/api/admin/settings", json={"mail_host": "smtp-relay.example.net", "mail_port": "2525", "mail_security": "starttls",
                                                 "mail_username": "login", "mail_from": "Station <station@example.org>"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.post("/api/credentials", json={"mail_password": "relay-secret"}).status_code, 200)
        self.assertTrue(c.get("/api/credentials").get_json()["set"]["mail_password"])
        self.assertNotIn("relay-secret", c.get("/api/admin/settings").get_data(as_text=True), "the settings answer never carries the password")
        self.assertTrue(n.mail_configured())
        cfg = n.mail_config()
        self.assertEqual((cfg["host"], cfg["port"], cfg["security"], cfg["username"], cfg["password"], cfg["sender"]),
                         ("smtp-relay.example.net", 2525, "starttls", "login", "relay-secret", "Station <station@example.org>"))

        calls = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None): calls.append(("connect", host, port))
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): calls.append(("starttls",))
            def login(self, u, p): calls.append(("login", u, p))
            def send_message(self, m): calls.append(("send", m["From"], m["To"], m["Subject"]))
        orig = n.smtplib.SMTP
        n.smtplib.SMTP = FakeSMTP
        try:
            ok, detail = n.send_email("you@example.com", "Hello", "body")
        finally:
            n.smtplib.SMTP = orig
        self.assertTrue(ok, detail)
        self.assertEqual(calls[0], ("connect", "smtp-relay.example.net", 2525))
        self.assertIn(("starttls",), calls)
        self.assertIn(("login", "login", "relay-secret"), calls)
        self.assertEqual(calls[-1][1:3], ("Station <station@example.org>", "you@example.com"))
        # The password reset follows the same switch.
        self.assertTrue(self.p._mail_configured())
        self.assertEqual(c.post("/api/admin/settings", json={"mail_host": ""}).status_code, 200)
        self.assertFalse(n.mail_configured(), "host cleared: back to not configured (no environment either)")
        self.assertEqual(self.p.app.test_client().post("/auth/password-reset", json={"email": "a@example.com"}).status_code, 501)

    def test_environment_is_the_fallback_only(self):
        os.environ["MAIL_SERVER"] = "env.example.net"; os.environ["MAIL_DEFAULT_SENDER"] = "env@example.org"
        try:
            cfg = self.n.mail_config()
            self.assertEqual((cfg["host"], cfg["sender"]), ("env.example.net", "env@example.org"))
            self.client.post("/api/admin/settings", json={"mail_host": "page.example.net"})
            self.assertEqual(self.n.mail_config()["host"], "page.example.net", "a value on the page wins over the environment")
        finally:
            os.environ.pop("MAIL_SERVER"); os.environ.pop("MAIL_DEFAULT_SENDER")


class LocalSeoTests(unittest.TestCase):
    def test_words_follow_the_country(self):
        import localize
        self.assertEqual(localize.local_seo("Uitgeest", "nl")[:2], ("nl", "Vliegtuiggeluid in Uitgeest"))
        self.assertEqual(localize.local_seo("Köln", "DE")[1], "Fluglärm in Köln")
        self.assertEqual(localize.local_seo("Palma", "es")[0], "es")
        self.assertEqual(localize.local_seo("Somewhere", "zz")[1], "Aircraft noise in Somewhere")
        self.assertIn("Vliegtuigherrie", localize.local_seo("Uitgeest", "nl")[2])

