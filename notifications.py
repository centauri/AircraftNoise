"""Operational alerts by email and ntfy.

Driven by the notify_* admin settings that the Notifications card in the admin
settings page writes. The server's health monitor and the outside watchdog
(scripts/watchdog.py) both deliver through here, so one recipient set once
applies to every alert. Nothing in this module imports the server.
"""

import logging
import os
import smtplib
import time
from email.message import EmailMessage

import requests

from config import Config

DEFAULTS = {
    "notify_enabled": "false",
    "notify_method": "ntfy",            # ntfy, email, both
    "notify_email": "",
    "notify_ntfy_url": "",              # https://ntfy.sh/your-topic
    "notify_ntfy_token": "",
    "notify_noise_stale_min": "15",
    "notify_aircraft_stale_min": "30",
    "notify_quota_percent": "90",
    "notify_noise_stale": "true",
    "notify_aircraft_stale": "true",
    "notify_recorder": "true",
    "notify_quota": "true",
    "notify_recovery": "true",
    # The mail server, set under Alerts. Empty values fall back to the MAIL_*
    # environment; the password is a secret (api_secret.mail_password).
    "mail_host": "",
    "mail_port": "587",
    "mail_security": "starttls",        # starttls, ssl, none
    "mail_username": "",
    "mail_from": "",
}

REALERT_S = 24 * 3600        # a condition that stays broken is reported again after a day
RATE_LIMIT_S = 3600          # the same title is not sent twice within an hour
NAME = "Aircraft Noise"


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# Set by the server: db.get_admin_setting, so the mail server can be read
# outside the alert path (the password reset).
settings_getter = None


def read_settings(get):
    """All notify_* and mail_* settings with defaults. `get` is db.get_admin_setting."""
    out = {key: (get(key, default) if get(key, None) not in (None, "") else default)
           for key, default in DEFAULTS.items()}
    out["mail_password"] = str(get("api_secret.mail_password", "") or "")
    return out


def mail_config(settings=None):
    """Host, port, security, username, password, sender: the settings first,
    the MAIL_* environment where a setting is empty."""
    if settings is None:
        settings = read_settings(settings_getter) if settings_getter else {}
    # The environment is read here, not from Config: Config fills in a Gmail
    # host nobody chose, and the tests change the environment while running.
    env = lambda key: (os.getenv(key) or "").strip()
    host = str(settings.get("mail_host") or env("MAIL_SERVER")).strip()
    try:
        port = int(str(settings.get("mail_port") or env("MAIL_PORT") or 587))
    except (TypeError, ValueError):
        port = 587
    env_tls = env("MAIL_USE_TLS").lower() in ("", "1", "true", "yes", "on")
    security = str(settings.get("mail_security") or ("starttls" if env_tls else "none")).strip().lower()
    username = str(settings.get("mail_username") or env("MAIL_USERNAME")).strip()
    password = str(settings.get("mail_password") or os.getenv("MAIL_PASSWORD") or "")
    sender = str(settings.get("mail_from") or env("MAIL_DEFAULT_SENDER") or username).strip()
    return {"host": host, "port": port, "security": security, "username": username, "password": password, "sender": sender}


def mail_configured(settings=None):
    cfg = mail_config(settings)
    return bool(cfg["host"] and cfg["sender"])


def _minutes(settings, key, fallback):
    try:
        return max(1, int(float(settings.get(key, fallback))))
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------- delivery

def send_email(to_address, subject, body, settings=None):
    """SMTP with the mail server from the settings (Alerts page), else the
    MAIL_* environment. Returns (ok, detail)."""
    if not to_address:
        return False, "no recipient"
    cfg = mail_config(settings)
    if not cfg["host"] or not cfg["sender"]:
        return False, "mail server not set: fill in the host and the From address under Admin → Alerts (or MAIL_SERVER and MAIL_DEFAULT_SENDER in the environment)"
    message = EmailMessage()
    message["From"] = cfg["sender"]
    message["To"] = to_address
    message["Subject"] = f"[{NAME}] {subject}"
    message.set_content(body)
    try:
        if cfg["security"] == "ssl":
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15)
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
        with smtp:
            if cfg["security"] == "starttls":
                smtp.starttls()
            if cfg["username"] and cfg["password"]:
                smtp.login(cfg["username"], cfg["password"])
            smtp.send_message(message)
        return True, f"email sent to {to_address}"
    except Exception as e:   # smtplib raises many kinds; the detail goes to the admin page
        return False, f"email failed: {e}"


def send_ntfy(url, token, subject, body, priority="default", tags=None):
    """One ntfy publish. Returns (ok, detail)."""
    if not url or not url.lower().startswith(("https://", "http://")):
        return False, "no ntfy topic URL"
    headers = {
        "Title": subject.encode("ascii", "replace").decode(),
        "Priority": priority,
        "Tags": ",".join(tags or ["airplane"]),
        "User-Agent": "plane-noise-dashboard/1.0 (+https://aircraftnoise.eu)",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        return False, f"ntfy failed: {e}"
    if 200 <= r.status_code < 300:
        return True, "ntfy published"
    return False, f"ntfy returned {r.status_code}"


def deliver(settings, subject, body, priority="default", tags=None, force=False):
    """Send by the configured method(s). Returns (delivered, detail).

    `force` sends even when notifications are switched off: the test button.
    """
    if not force and not truthy(settings.get("notify_enabled")):
        return False, "notifications are off"
    method = str(settings.get("notify_method") or "ntfy").lower()
    want_email = method in ("email", "both")
    want_ntfy = method in ("ntfy", "both")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = f"{body}\n\n{NAME} · {stamp}"
    results = []
    delivered = False
    if want_email:
        ok, detail = send_email(settings.get("notify_email", ""), subject, text, settings)
        delivered = delivered or ok
        results.append(detail)
    if want_ntfy:
        ok, detail = send_ntfy(settings.get("notify_ntfy_url", ""), settings.get("notify_ntfy_token", ""),
                               subject, text, priority, tags)
        delivered = delivered or ok
        results.append(detail)
    if not results:
        results.append("no method configured")
    detail = "; ".join(results)
    (logging.info if delivered else logging.warning)("Alert %s: %s (%s)", "sent" if delivered else "not sent", subject, detail)
    return delivered, detail


# ---------------------------------------------------------------- checks

class Finding:
    __slots__ = ("key", "kind", "title", "body", "failing", "recover", "priority")

    def __init__(self, key, kind, title, body, failing, recover=True, priority="high"):
        self.key = key
        self.kind = kind          # the notify_<kind> switch that allows it
        self.title = title
        self.body = body
        self.failing = failing
        self.recover = recover
        self.priority = priority


def _age_text(seconds):
    if seconds is None:
        return "never"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def evaluate(snapshot, settings):
    """Turn a health snapshot into findings. Each finding says whether it is failing now."""
    uptime = snapshot.get("uptime_s") or 0
    findings = []

    noise_limit = _minutes(settings, "notify_noise_stale_min", 15) * 60
    noise_age = snapshot.get("noise_age_s")
    noise_failing = (noise_age is None and uptime > noise_limit) or (noise_age is not None and noise_age > noise_limit)
    findings.append(Finding(
        "noise", "noise_stale", "Noise sensor offline",
        f"No new noise sample for {_age_text(noise_age)}. Limit is {noise_limit // 60} min.\n"
        f"Feed: {snapshot.get('noise_feed') or '?'}",
        noise_failing))

    aircraft_limit = _minutes(settings, "notify_aircraft_stale_min", 30) * 60
    aircraft_age = snapshot.get("aircraft_ok_age_s")
    aircraft_failing = (aircraft_age is None and uptime > aircraft_limit) or (aircraft_age is not None and aircraft_age > aircraft_limit)
    findings.append(Finding(
        "aircraft", "aircraft_stale", "Aircraft feed failing",
        f"No successful position poll for {_age_text(aircraft_age)}. Limit is {aircraft_limit // 60} min.\n"
        f"Feed: {snapshot.get('aircraft_feed') or '?'}\nLast error: {snapshot.get('aircraft_last_error') or 'none'}",
        aircraft_failing))

    loop_age = snapshot.get("loop_age_s")
    interval = snapshot.get("aircraft_interval_s") or 300
    loop_failing = loop_age is not None and loop_age > max(3 * interval, 300)
    db_failing = snapshot.get("db_ok") is False
    findings.append(Finding(
        "recorder", "recorder", "Recorder stopped",
        f"Polling loop last ran {_age_text(loop_age)} ago.\nDatabase: {'ok' if not db_failing else snapshot.get('db_error')}",
        loop_failing or db_failing, priority="urgent"))

    limit = snapshot.get("opensky_daily_limit") or 0
    used = snapshot.get("opensky_daily_requests") or 0
    percent = _minutes(settings, "notify_quota_percent", 90)
    quota_failing = bool(limit) and used * 100 >= limit * percent
    findings.append(Finding(
        "quota", "quota", "OpenSky daily allowance nearly used",
        f"{used} of {limit} calls used today ({percent}% limit). Positions stop when it runs out.",
        quota_failing, recover=False, priority="default"))
    return findings


class Alerter:
    """Alert once when a condition starts failing, again after a day, and once when it recovers."""

    def __init__(self, deliver_fn=deliver, realert_s=REALERT_S, rate_limit_s=RATE_LIMIT_S):
        self._deliver = deliver_fn
        self._realert_s = realert_s
        self._rate_limit_s = rate_limit_s
        self.state = {}          # key -> {'since': ts, 'alerted_at': ts or None, 'title': str}
        self._last_sent = {}     # title -> ts

    def _send(self, settings, title, body, priority, now):
        last = self._last_sent.get(title)
        if last is not None and now - last < self._rate_limit_s:
            return False
        delivered, _detail = self._deliver(settings, title, body, priority=priority)
        if delivered:
            self._last_sent[title] = now
        return delivered

    def process(self, findings, settings, now=None):
        now = time.time() if now is None else now
        enabled = truthy(settings.get("notify_enabled"))
        for f in findings:
            record = self.state.get(f.key)
            if f.failing:
                if record is None:
                    record = {"since": now, "alerted_at": None, "title": f.title}
                    self.state[f.key] = record
                allowed = enabled and truthy(settings.get(f"notify_{f.kind}", "true"))
                due = record["alerted_at"] is None or now - record["alerted_at"] >= self._realert_s
                if allowed and due and self._send(settings, f.title, f.body, f.priority, now):
                    record["alerted_at"] = now
            elif record is not None:
                if record["alerted_at"] is not None and f.recover and enabled and truthy(settings.get("notify_recovery", "true")):
                    self._send(settings, f"{f.title}: back to normal", f"{f.title} cleared after {_age_text(now - record['since'])}.",
                               "default", now)
                del self.state[f.key]
        return {key: {"since": r["since"], "alerted_at": r["alerted_at"], "title": r["title"]}
                for key, r in self.state.items()}
