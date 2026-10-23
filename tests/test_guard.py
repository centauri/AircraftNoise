import unittest

import guard


class FakeStore:
    def __init__(self):
        self.saved, self.dropped = {}, []

    def save_ban(self, ip, until, reason, bans):
        self.saved[ip] = (until, reason, bans)

    def load_bans(self):
        return [(ip, v[0], v[2]) for ip, v in self.saved.items()]

    def drop_ban(self, ip):
        self.dropped.append(ip)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_000_000.0]
        self.store = FakeStore()
        self.g = guard.Guard(store=self.store, clock=lambda: self.now[0], resolve=lambda ip: ip == "66.249.1.1")

    def test_classify(self):
        self.assertEqual(guard.classify("/"), "page")
        self.assertEqual(guard.classify("/callsign.html"), "page")
        self.assertEqual(guard.classify("/recent_events"), "api")
        self.assertEqual(guard.classify("/api/live_aircraft"), "api")
        self.assertEqual(guard.classify("/api/admin/settings"), "exempt")
        self.assertEqual(guard.classify("/admin/settings"), "exempt")
        self.assertEqual(guard.classify("/static/js/x.js"), "exempt")
        self.assertEqual(guard.classify("/api/health"), "exempt")
        self.assertEqual(guard.classify("/auth/login", "POST"), "exempt")
        self.assertEqual(guard.classify("/trap/all"), "trap")

    def test_people_and_the_live_page_are_not_touched(self):
        for _ in range(40):
            self.assertTrue(self.g.check("1.1.1.1", "page", "Mozilla/5.0").ok)
            self.now[0] += 2
        # One live tab: six JSON calls every ten seconds for an hour.
        for _ in range(360):
            for _ in range(6):
                self.assertTrue(self.g.check("1.1.1.1", "api", "Mozilla/5.0").ok)
            self.now[0] += 10
        self.assertTrue(self.g.check("127.0.0.1", "trap", "python-requests").ok, "the local machine is never judged")

    def test_budget_then_escalating_bans(self):
        for _ in range(guard.PAGES_PER_MINUTE):
            self.g.check("2.2.2.2", "page", "Mozilla/5.0")
        v = self.g.check("2.2.2.2", "page", "Mozilla/5.0")
        self.assertEqual(v.status, 429)
        self.assertGreater(v.retry_after, 0)
        banned = None
        for _ in range(300):
            v = self.g.check("2.2.2.2", "page", "Mozilla/5.0")
            if v.status == 403:
                banned = v
                break
        self.assertIsNotNone(banned)
        self.assertIn("2.2.2.2", self.store.saved)
        self.assertEqual(self.store.saved["2.2.2.2"][2], 1)
        self.now[0] += 14 * 60
        self.assertEqual(self.g.check("2.2.2.2", "page", "Mozilla/5.0").status, 403)
        self.now[0] += 2 * 60
        self.assertTrue(self.g.check("2.2.2.2", "page", "Mozilla/5.0").ok, "a fresh budget after the ban")
        # Second ban lasts an hour.
        self.g.check("2.2.2.2", "trap", "Mozilla/5.0")
        self.assertEqual(self.store.saved["2.2.2.2"][2], 2)
        self.assertAlmostEqual(self.store.saved["2.2.2.2"][0], self.now[0] + 3600, delta=1)

    def test_trap_and_scripts(self):
        self.assertEqual(self.g.check("3.3.3.3", "trap", "Mozilla/5.0").reason, "trap")
        self.assertEqual(self.g.check("3.3.3.3", "page", "Mozilla/5.0").status, 403)
        last = None
        for _ in range(guard.SCRIPT_PAGES_PER_HOUR + 1):
            last = self.g.check("4.4.4.4", "page", "python-requests/2.32")
        self.assertEqual(last.reason, "script")
        self.assertTrue(self.g.check("4.4.4.4", "api", "python-requests/2.32").ok, "scripts may use the JSON endpoints")

    def test_crawlers_and_misses(self):
        for _ in range(guard.PAGES_PER_MINUTE * 3):
            self.assertTrue(self.g.check("66.249.1.1", "page", "Mozilla/5.0 (compatible; Googlebot/2.1)").ok)
        self.assertEqual(self.g.check("5.5.5.5", "page", "Mozilla/5.0 (compatible; Googlebot/2.1)").status, 200)
        for _ in range(guard.MISS_LIMIT * guard.STRIKES_TO_BAN):
            self.g.miss("6.6.6.6")
        self.assertEqual(self.g.check("6.6.6.6", "page", "Mozilla/5.0").status, 403)

    def test_bans_survive_a_restart_and_unban(self):
        self.g.check("7.7.7.7", "trap", "x")
        again = guard.Guard(store=self.store, clock=lambda: self.now[0], resolve=lambda ip: False)
        self.assertEqual(again.check("7.7.7.7", "page", "Mozilla/5.0").status, 403)
        self.assertEqual([b["ip"] for b in again.bans()], ["7.7.7.7"])
        again.unban("7.7.7.7")
        self.assertTrue(again.check("7.7.7.7", "page", "Mozilla/5.0").ok)
        self.assertIn("7.7.7.7", self.store.dropped)

    def test_client_ip_and_disabled(self):
        self.assertEqual(guard.client_ip({"CF-Connecting-IP": "9.9.9.9", "X-Forwarded-For": "8.8.8.8"}, "10.0.0.1"), "9.9.9.9")
        self.assertEqual(guard.client_ip({"X-Forwarded-For": "8.8.8.8, 10.0.0.2"}, "10.0.0.1"), "8.8.8.8")
        self.assertEqual(guard.client_ip({}, "10.0.0.1"), "10.0.0.1")
        self.g.enabled = False
        self.assertTrue(self.g.check("3.3.3.3", "trap", "x").ok)


if __name__ == "__main__":
    unittest.main()


class GuardHookTests(unittest.TestCase):
    """The hook in the server: remote addresses are judged, the machine itself and the admin are not."""

    @classmethod
    def setUpClass(cls):
        import os
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        os.environ["PN_DB_PATH"] = os.path.join(cls.tmp, "guard.db")
        import proxy_server
        cls.server = proxy_server
        proxy_server._polling_started = True
        proxy_server.app.config["TESTING"] = True
        proxy_server.scraper_guard = guard.Guard(store=None, resolve=lambda ip: False)
        cls.client = proxy_server.app.test_client()

    def test_trap_blocks_a_remote_address_but_not_the_machine_or_the_admin(self):
        remote = {"REMOTE_ADDR": "203.0.113.9"}
        self.assertEqual(self.client.get("/trap/events", environ_base=remote).status_code, 403)
        page = self.client.get("/", environ_base=remote)
        self.assertEqual(page.status_code, 403)
        self.assertIn("Retry-After", page.headers)
        api = self.client.get("/recent_events", environ_base=remote)
        self.assertEqual(api.status_code, 403)
        self.assertEqual(api.get_json()["ok"], False)
        self.assertNotEqual(self.client.get("/admin/login", environ_base=remote).status_code, 403, "logins are exempt")
        self.assertNotEqual(self.client.get("/api/health", environ_base=remote).status_code, 403, "the watchdog is exempt")
        self.assertNotEqual(self.client.get("/trap/events", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, 403)
        forwarded = self.client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.2"}, headers={"CF-Connecting-IP": "203.0.113.9"})
        self.assertEqual(forwarded.status_code, 403, "the address behind Cloudflare is the one judged")
        self.server.scraper_guard.unban("203.0.113.9")
        self.assertNotEqual(self.client.get("/", environ_base=remote).status_code, 403)


class AllowListTests(unittest.TestCase):
    def test_allowed_addresses_are_never_judged(self):
        g = guard.Guard(clock=lambda: 1000.0)
        g.set_allow("203.0.113.7, 192.168.1.0/24, not-an-address")
        for _ in range(300):
            self.assertTrue(g.check("203.0.113.7", "page", "curl/8").ok)
        self.assertTrue(g.check("192.168.1.44", "trap", "Mozilla/5.0").ok)
        for _ in range(100):
            g.miss("203.0.113.7")
        self.assertTrue(g.check("203.0.113.7", "page", "Mozilla/5.0").ok)
        self.assertFalse(g.check("198.51.100.9", "trap", "Mozilla/5.0").ok)
        g.set_allow("")
        self.assertFalse(g.allowed("203.0.113.7"))

