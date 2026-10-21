"""Visitor statistics without personal data, bounded for years.

Every request to a public page or JSON endpoint is counted in memory and
written out once a minute as daily totals: views and visitors per day and
per hour, and per day the top pages, referrer sites, languages, devices,
countries, JSON endpoints and bot kinds. A visitor is a hash of address,
user agent and a salt that changes every day; the hash lives in memory for
that day and is never written, so no address and no lasting identifier is
stored anywhere. Nothing per request is written. The tables are capped:
a day holds at most MAX_KEYS keys per kind, hour rows and per-kind rows
are dropped after RETAIN_DAYS, and the daily totals (one row a day) stay.
Nothing in here imports the server.
"""

import hashlib
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

FLUSH_SECONDS = 60
ONLINE_SECONDS = 5 * 60
MAX_KEYS = 100          # per kind per day; the rest lands in "other"
RETAIN_DAYS = 400       # hours and per-kind rows; day totals are kept forever
KINDS = ("page", "referrer", "campaign", "language", "device", "country", "city", "api", "bot")

PAGE_PATHS = {"/": "/", "/index.html": "/", "/callsign.html": "/callsign", "/types.html": "/types",
              "/community": "/community", "/seo-content.html": "/about"}


def normalise_path(path, kind):
    """Pages fold to a handful of names; JSON keeps its endpoint, never its query."""
    path = (path or "/").split("?", 1)[0]
    if kind == "page":
        return PAGE_PATHS.get(path, "/other")
    if path.startswith("/api/callsigns/"):
        return "/api/callsigns/*"
    if path.startswith("/api/stats/"):
        return path
    return path[:60]


def referrer_site(referrer, own_host):
    """The referring site, or 'direct'. Own pages and empty referrers are direct."""
    raw = (referrer or "").strip()
    if not raw:
        return "direct"
    host = (urlparse(raw).hostname or "").lower()
    if not host or host == (own_host or "").lower() or host.endswith("." + (own_host or "").lower()):
        return "direct"
    return host[:60]


def language_tag(accept_language):
    """The primary language of the browser: 'nl', 'en', 'de'."""
    first = (accept_language or "").split(",", 1)[0].strip().lower()
    tag = first.split(";", 1)[0].split("-", 1)[0]
    return tag[:8] if tag.isalpha() else "unknown"


def device_class(user_agent):
    ua = (user_agent or "").lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if "mobile" in ua or "android" in ua or "iphone" in ua:
        return "phone"
    return "desktop"


def campaign(query):
    """'source / campaign' from utm tags in the page address, or None."""
    from urllib.parse import parse_qs
    params = parse_qs(query or "", keep_blank_values=False)
    source = (params.get("utm_source") or [""])[0].strip().lower()[:40]
    if not source:
        return None
    name = (params.get("utm_campaign") or [""])[0].strip().lower()[:40]
    return "{0} / {1}".format(source, name) if name else source


def country_code(headers):
    code = (headers.get("CF-IPCountry") or headers.get("Cf-Ipcountry") or headers.get("CF-Country") or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else "unknown"


class Day:
    """One day's counters, in memory until flushed."""

    __slots__ = ("key", "views", "api_views", "visitors", "hours", "dims", "salt")

    def __init__(self, key):
        self.key = key
        self.views = 0
        self.api_views = 0
        self.visitors = set()
        self.hours = {}       # hour start (unix time) -> [views, set(visitor hashes)]
        self.dims = {k: {} for k in KINDS}
        self.salt = secrets.token_hex(16)


class Stats:
    """The counter. `store` is a PlaneNoiseDB; it may be None in tests."""

    def __init__(self, store=None, clock=time.time, tz=None):
        self.store = store
        self.clock = clock
        self.tz = tz or timezone.utc
        self._lock = threading.Lock()
        self._days = {}
        self._online = {}      # visitor hash -> last seen
        self._last_flush = clock()
        self._dirty = False

    # -- counting ------------------------------------------------------------

    def _local(self, ts):
        return datetime.fromtimestamp(ts, self.tz)

    def _day(self, when):
        key = when.strftime("%Y-%m-%d")
        d = self._days.get(key)
        if d is None:
            d = Day(key)
            self._days[key] = d
        return d

    def visitor_hash(self, day, ip, user_agent):
        return hashlib.sha256("{0}|{1}|{2}".format(day.salt, ip or "", user_agent or "").encode()).hexdigest()[:16]

    def count(self, kind, path, ip, user_agent, headers=None, own_host="", bot=None, query="", country=None, city=None):
        """Record one request. kind: 'page' or 'api'. bot: None for a person, else a label.
        country and city come from the caller's lookup when there is no edge header."""
        headers = headers or {}
        now = self.clock()
        when = self._local(now)
        with self._lock:
            d = self._day(when)
            self._dirty = True
            if bot:
                _bump(d.dims["bot"], bot)
                return
            if kind == "api":
                d.api_views += 1
                _bump(d.dims["api"], normalise_path(path, "api"))
                return
            d.views += 1
            vh = self.visitor_hash(d, ip, user_agent)
            d.visitors.add(vh)
            hour_ts = int(when.replace(minute=0, second=0, microsecond=0).timestamp())
            hour = d.hours.setdefault(hour_ts, [0, set()])
            hour[0] += 1
            hour[1].add(vh)
            self._online[vh] = now
            _bump(d.dims["page"], normalise_path(path, "page"))
            _bump(d.dims["referrer"], referrer_site(headers.get("Referer"), own_host))
            _bump(d.dims["language"], language_tag(headers.get("Accept-Language")))
            _bump(d.dims["device"], device_class(user_agent))
            cc = country_code(headers)
            if cc == "unknown" and country:
                cc = str(country).upper()[:2]
            _bump(d.dims["country"], cc)
            if city:
                _bump(d.dims["city"], str(city)[:60])
            tag = campaign(query)
            if tag:
                _bump(d.dims["campaign"], tag)

    def online(self):
        cutoff = self.clock() - ONLINE_SECONDS
        with self._lock:
            for vh in [vh for vh, seen in self._online.items() if seen < cutoff]:
                del self._online[vh]
            return len(self._online)

    # -- flushing ------------------------------------------------------------

    def flush_if_due(self):
        if self.clock() - self._last_flush >= FLUSH_SECONDS:
            self.flush()

    def flush(self):
        """Write every day's totals, then drop days that are over."""
        with self._lock:
            self._last_flush = self.clock()
            if not self._dirty or self.store is None:
                return
            today = self._local(self.clock()).strftime("%Y-%m-%d")
            days = list(self._days.values())
            self._dirty = False
        for d in days:
            try:
                self.store.save_stats_day(d.key, d.views, d.api_views, len(d.visitors),
                                          {h: (v[0], len(v[1])) for h, v in d.hours.items()},
                                          {k: _capped(v) for k, v in d.dims.items()})
            except Exception:
                logging.exception("Visitor statistics could not be saved")
                with self._lock:
                    self._dirty = True
                return
        with self._lock:
            for key in [k for k in self._days if k < today]:
                del self._days[key]

    def retain(self):
        """Drop hour and per-kind rows older than RETAIN_DAYS; day totals stay."""
        if self.store is not None:
            try:
                self.store.prune_stats(RETAIN_DAYS)
            except Exception:
                logging.exception("Visitor statistics could not be pruned")


def _bump(counter, key):
    counter[key] = counter.get(key, 0) + 1


def _capped(counter):
    """At most MAX_KEYS keys; the tail is summed into 'other'."""
    if len(counter) <= MAX_KEYS:
        return dict(counter)
    items = sorted(counter.items(), key=lambda kv: -kv[1])
    kept = dict(items[:MAX_KEYS])
    kept["other"] = kept.get("other", 0) + sum(n for _, n in items[MAX_KEYS:])
    return kept


# -- reading --------------------------------------------------------------

def noise_against_visits(hours, passes_by_hour):
    """Do people look when it is loud? Compares the views in hours with a loud
    pass against hours without, within the same hour of the day, so evenings
    are not compared with nights. hours: [(ts, views, visitors)], passes_by_hour:
    {ts: (passes, loud)}. Returns the ratio and the counts behind it."""
    by_hod = {}
    for ts, views, _ in hours:
        hod = datetime.fromtimestamp(ts, timezone.utc).hour
        loud = passes_by_hour.get(ts, (0, 0))[1] > 0
        slot = by_hod.setdefault(hod, {"loud": [0, 0], "quiet": [0, 0]})
        bucket = slot["loud" if loud else "quiet"]
        bucket[0] += views
        bucket[1] += 1
    loud_rate, quiet_rate, loud_hours, quiet_hours = 0.0, 0.0, 0, 0
    for slot in by_hod.values():
        if slot["loud"][1] and slot["quiet"][1]:
            loud_rate += slot["loud"][0] / slot["loud"][1]
            quiet_rate += slot["quiet"][0] / slot["quiet"][1]
            loud_hours += slot["loud"][1]
            quiet_hours += slot["quiet"][1]
    ratio = (loud_rate / quiet_rate) if quiet_rate > 0 else None
    return {"ratio": round(ratio, 2) if ratio is not None else None, "loud_hours": loud_hours, "quiet_hours": quiet_hours}
