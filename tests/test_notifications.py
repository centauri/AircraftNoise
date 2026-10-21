import json
import os
import tempfile
import unittest

import notifications
import proxy_server
from database import PlaneNoiseDB


class _Resp:
    def __init__(self, status):
        self.status_code = status


def _settings(**overrides):
    base = dict(notifications.DEFAULTS)
    base.update({"notify_enabled": "true", "notify_method": "ntfy", "notify_ntfy_url": "https://ntfy.sh/t"})
    base.update(overrides)
    return base


class EvaluateTests(unittest.TestCase):
    def test_fresh_feeds_do_not_fail(self):
        snap = {"uptime_s": 9999, "noise_age_s": 60, "aircraft_ok_age_s": 40, "loop_age_s": 5,
                "aircraft_interval_s": 30, "db_ok": True, "opensky_daily_requests": 100, "opensky_daily_limit": 4000}
        self.assertEqual([f.key for f in notifications.evaluate(snap, _settings()) if f.failing], [])

    def test_stale_noise_and_dead_loop_fail(self):
        snap = {"uptime_s": 9999, "noise_age_s": 16 * 60, "aircraft_ok_age_s": 40, "loop_age_s": 1000,
                "aircraft_interval_s": 30, "db_ok": True, "opensky_daily_requests": 3700, "opensky_daily_limit": 4000}
        failing = {f.key: f for f in notifications.evaluate(snap, _settings()) if f.failing}
        self.assertEqual(set(failing), {"noise", "recorder", "quota"})
        self.assertIn("16 min", failing["noise"].body)

    def test_startup_grace_before_first_samples(self):
        snap = {"uptime_s": 120, "noise_age_s": None, "aircraft_ok_age_s": None, "loop_age_s": 5,
                "aircraft_interval_s": 30, "db_ok": True}
        self.assertEqual([f.key for f in notifications.evaluate(snap, _settings()) if f.failing], [])
        snap["uptime_s"] = 3600
        self.assertEqual({f.key for f in notifications.evaluate(snap, _settings()) if f.failing}, {"noise", "aircraft"})


class AlerterTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.alerter = notifications.Alerter(deliver_fn=self._deliver, realert_s=1000, rate_limit_s=10)

    def _deliver(self, settings, title, body, priority="default", tags=None, force=False):
        self.sent.append((title, priority))
        return True, "ok"

    def _find(self, failing):
        return [notifications.Finding("noise", "noise_stale", "Noise sensor offline", "body", failing)]

    def test_once_then_recovery_then_realert(self):
        s = _settings()
        self.alerter.process(self._find(True), s, now=0)
        self.alerter.process(self._find(True), s, now=100)
        self.assertEqual(self.sent, [("Noise sensor offline", "high")])
        self.alerter.process(self._find(True), s, now=1100)
        self.assertEqual(len(self.sent), 2)                      # reminder after realert_s
        self.alerter.process(self._find(False), s, now=1200)
        self.assertEqual(self.sent[-1][0], "Noise sensor offline: back to normal")
        self.assertEqual(self.alerter.state, {})

    def test_disabled_type_is_tracked_but_silent(self):
        s = _settings(notify_noise_stale="false")
        self.alerter.process(self._find(True), s, now=0)
        self.assertEqual(self.sent, [])
        self.assertIn("noise", self.alerter.state)
        self.alerter.process(self._find(False), s, now=5)
        self.assertEqual(self.sent, [])                          # never alerted, so no recovery message

    def test_off_switch_blocks_everything(self):
        self.alerter.process(self._find(True), _settings(notify_enabled="false"), now=0)
        self.assertEqual(self.sent, [])


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self._post = notifications.requests.post
        self._smtp = notifications.smtplib.SMTP
        self.posts = []
        notifications.requests.post = lambda url, **kw: (self.posts.append((url, kw)), _Resp(200))[1]

    def tearDown(self):
        notifications.requests.post = self._post
        notifications.smtplib.SMTP = self._smtp

    def test_ntfy_headers(self):
        ok, detail = notifications.deliver(_settings(notify_ntfy_token="tk_1"), "Recorder stopped", "loop dead", priority="urgent")
        self.assertTrue(ok, detail)
        url, kw = self.posts[0]
        self.assertEqual(url, "https://ntfy.sh/t")
        self.assertEqual(kw["headers"]["Title"], "Recorder stopped")
        self.assertEqual(kw["headers"]["Priority"], "urgent")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer tk_1")
        self.assertIn(b"loop dead", kw["data"])

    def test_off_unless_forced(self):
        s = _settings(notify_enabled="false")
        self.assertFalse(notifications.deliver(s, "x", "y")[0])
        self.assertTrue(notifications.deliver(s, "x", "y", force=True)[0])

    def test_email_without_server_reports_why(self):
        orig = (notifications.Config.MAIL_DEFAULT_SENDER, notifications.Config.MAIL_USERNAME)
        notifications.Config.MAIL_DEFAULT_SENDER = None
        notifications.Config.MAIL_USERNAME = None
        try:
            ok, detail = notifications.deliver(_settings(notify_method="email", notify_email="a@b.c"), "x", "y")
        finally:
            notifications.Config.MAIL_DEFAULT_SENDER, notifications.Config.MAIL_USERNAME = orig
        self.assertFalse(ok)
        self.assertIn("MAIL_DEFAULT_SENDER", detail)


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self._orig = (proxy_server.db, proxy_server.noise_data_history, proxy_server._last_poll_tick,
                      proxy_server._last_aircraft_ok, proxy_server._polling_started)
        proxy_server.db = self.db
        proxy_server._polling_started = True
        proxy_server.noise_data_history = []
        proxy_server._last_poll_tick = None
        proxy_server._last_aircraft_ok = None

    def tearDown(self):
        (proxy_server.db, proxy_server.noise_data_history, proxy_server._last_poll_tick,
         proxy_server._last_aircraft_ok, proxy_server._polling_started) = self._orig
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_health_reports_ages_and_failing_keys(self):
        import time
        proxy_server.noise_data_history = [{"timestamp": time.time() - 30, "value": 40.0}]
        proxy_server._last_aircraft_ok = time.time() - 3600
        proxy_server._last_poll_tick = time.time()
        client = proxy_server.app.test_client()
        h = client.get("/api/health").get_json()
        self.assertTrue(h["ok"])
        self.assertLess(h["noise_age_s"], 60)
        self.assertIn("aircraft", h["failing"])
        self.assertNotIn("noise", h["failing"])
        self.assertNotIn("notify_ntfy_token", json.dumps(h))


if __name__ == "__main__":
    unittest.main()
