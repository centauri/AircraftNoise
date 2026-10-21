"""Scraper guard for the public side of a station.

People read a few pages a minute from a browser, and the live page polls its
JSON a few times every ten seconds. Scrapers read hundreds of pages from
scripts and follow links people cannot see. The guard counts page and JSON
requests per client over a minute and an hour, answers 429 with Retry-After
when a budget is spent, and bans a client that keeps going, follows the
trap, or reads pages with a script. Bans escalate and are kept in the
database across restarts. Admin pages, logins and the local machine are
never touched; the watchdog and the admin's own browser keep working.
Nothing in here imports the server.
"""

import logging
import socket
import threading
import time

TRAP_PREFIX = "/trap/"
PAGES_PER_MINUTE = 60
PAGES_PER_HOUR = 600
API_PER_MINUTE = 300       # the live page polls several endpoints every 10 s
API_PER_HOUR = 6000
SCRIPT_PAGES_PER_HOUR = 10
MISS_LIMIT = 30
STRIKES_TO_BAN = 3
BAN_STEPS = (15 * 60, 3600, 6 * 3600, 24 * 3600, 7 * 24 * 3600)
CRAWLER_FACTOR = 10

SCRIPT_AGENTS = ("python-requests", "python-urllib", "aiohttp", "httpx/", "scrapy", "go-http-client", "curl/", "wget/",
                 "libwww", "java/", "okhttp", "node-fetch", "axios/", "php/", "guzzle", "ruby", "perl",
                 "headlesschrome", "phantomjs", "selenium", "puppeteer", "playwright")
CRAWLER_AGENTS = ("googlebot", "bingbot", "duckduckbot", "applebot", "yandex", "slurp")
CRAWLER_DOMAINS = (".googlebot.com", ".google.com", ".search.msn.com", ".crawl.yahoo.net", ".duckduckgo.com",
                   ".applebot.apple.com", ".yandex.ru", ".yandex.net")
EXEMPT_PREFIXES = ("/static/", "/admin", "/auth/", "/api/admin/", "/api/auth/", "/setup", "/api/setup/", "/api/health")
EXEMPT_PATHS = ("/robots.txt", "/favicon.ico", "/sitemap.xml")


def is_script(user_agent):
    """A library or tool, never a person's browser."""
    agent = (user_agent or "").lower()
    if not agent:
        return True
    return any(marker in agent for marker in SCRIPT_AGENTS)


def looks_like_crawler(user_agent):
    agent = (user_agent or "").lower()
    return any(marker in agent for marker in CRAWLER_AGENTS)


def classify(path, method="GET", has_key=False):
    """static, exempt, trap, api or page."""
    if path.startswith(TRAP_PREFIX):
        return "trap"
    if path in EXEMPT_PATHS or any(path.startswith(p) for p in EXEMPT_PREFIXES):
        return "exempt"
    if method != "GET" or has_key:
        return "exempt"
    if path.startswith("/api/") or path in ("/recent_events", "/visitor/settings"):
        return "api"
    return "page"


class _Window:
    __slots__ = ("start", "count")

    def __init__(self):
        self.start = 0.0
        self.count = 0

    def hit(self, now, span):
        if now - self.start >= span:
            self.start, self.count = now, 0
        self.count += 1
        return self.count


class _Client:
    __slots__ = ("page_min", "page_hour", "api_min", "api_hour", "script_pages", "misses",
                 "strikes", "bans", "last_strike", "banned_until", "seen")

    def __init__(self):
        self.page_min, self.page_hour = _Window(), _Window()
        self.api_min, self.api_hour = _Window(), _Window()
        self.script_pages, self.misses = _Window(), _Window()
        self.strikes = 0
        self.bans = 0
        self.last_strike = 0.0
        self.banned_until = 0.0
        self.seen = 0.0

    def reset_budgets(self):
        self.page_min, self.page_hour = _Window(), _Window()
        self.api_min, self.api_hour = _Window(), _Window()
        self.script_pages = _Window()


class Verdict:
    __slots__ = ("status", "retry_after", "reason", "crawler")

    def __init__(self, status=200, retry_after=0, reason="", crawler=False):
        self.status, self.retry_after, self.reason, self.crawler = status, int(retry_after), reason, crawler

    @property
    def ok(self):
        return self.status == 200


class Guard:
    """In-memory counters, bans mirrored to `store` (an object with
    save_ban(ip, until, reason, bans), load_bans() -> [(ip, until, bans)],
    drop_ban(ip)) when one is given."""

    def __init__(self, store=None, clock=time.time, resolve=None):
        self.store = store
        self.clock = clock
        self.resolve = resolve if resolve is not None else self._verified_crawler
        self.enabled = True
        self._lock = threading.Lock()
        self._clients = {}
        self._crawlers = {}
        if store is not None:
            try:
                for ip, until, bans in store.load_bans():
                    c = self._client(ip)
                    c.banned_until, c.bans, c.last_strike = float(until), int(bans), float(until)
            except Exception:
                logging.exception("Guard could not load the bans")

    def _client(self, ip):
        c = self._clients.get(ip)
        if c is None:
            c = _Client()
            self._clients[ip] = c
        return c

    # -- decisions -----------------------------------------------------------

    def check(self, ip, kind, user_agent=""):
        """Record one request and decide. kind is from classify()."""
        if not self.enabled or kind in ("exempt", "static") or not ip or ip in ("127.0.0.1", "::1"):
            return Verdict()
        now = self.clock()
        crawler = kind == "page" and looks_like_crawler(user_agent) and bool(self.resolve(ip))
        with self._lock:
            c = self._client(ip)
            c.seen = now
            if now < c.banned_until:
                return Verdict(403, c.banned_until - now, "banned")
            if c.banned_until:
                c.banned_until = 0.0
                c.reset_budgets()
            if kind == "trap":
                self._ban(ip, c, now, "trap")
                return Verdict(403, c.banned_until - now, "trap")
            factor = CRAWLER_FACTOR if crawler else 1
            if kind == "page":
                minute, hour = c.page_min.hit(now, 60), c.page_hour.hit(now, 3600)
                per_min, per_hour = PAGES_PER_MINUTE * factor, PAGES_PER_HOUR * factor
                if not crawler and is_script(user_agent) and c.script_pages.hit(now, 3600) > SCRIPT_PAGES_PER_HOUR:
                    self._strike(ip, c, now, "script user agent on pages")
                    return Verdict(403, 3600, "script")
                starts = (c.page_min.start, c.page_hour.start)
            else:
                minute, hour = c.api_min.hit(now, 60), c.api_hour.hit(now, 3600)
                per_min, per_hour = API_PER_MINUTE * factor, API_PER_HOUR * factor
                starts = (c.api_min.start, c.api_hour.start)
            if minute > per_min or hour > per_hour:
                over = hour - per_hour if hour > per_hour else minute - per_min
                retry = (3600 - (now - starts[1])) if hour > per_hour else (60 - (now - starts[0]))
                if over % 50 == 1:
                    self._strike(ip, c, now, "over the budget")
                    if now < c.banned_until:
                        return Verdict(403, c.banned_until - now, "banned")
                return Verdict(429, max(1, retry), "budget")
            return Verdict(crawler=crawler)

    def miss(self, ip):
        """A refused answer (401, 403, 429) the handlers produced."""
        if not self.enabled or not ip or ip in ("127.0.0.1", "::1"):
            return
        now = self.clock()
        with self._lock:
            c = self._client(ip)
            c.seen = now
            if c.misses.hit(now, 3600) % MISS_LIMIT == 0:
                self._strike(ip, c, now, "refused answers")

    def _strike(self, ip, c, now, reason):
        if now - c.last_strike > 24 * 3600:
            c.strikes = 0
        c.strikes += 1
        c.last_strike = now
        logging.warning("Guard: strike %d for %s (%s)", c.strikes, ip, reason)
        if c.strikes >= STRIKES_TO_BAN:
            self._ban(ip, c, now, reason)

    def _ban(self, ip, c, now, reason):
        if now - c.last_strike > 7 * 24 * 3600:
            c.bans = 0
        c.bans += 1
        c.strikes = 0
        c.last_strike = now
        length = BAN_STEPS[min(c.bans, len(BAN_STEPS)) - 1]
        c.banned_until = now + length
        logging.warning("Guard: banned %s for %ds (%s)", ip, length, reason)
        if self.store is not None:
            try:
                self.store.save_ban(ip, c.banned_until, reason, c.bans)
            except Exception:
                logging.exception("Guard could not save the ban")

    # -- admin ---------------------------------------------------------------

    def bans(self):
        now = self.clock()
        with self._lock:
            return sorted(
                ({"ip": ip, "until": c.banned_until, "bans": c.bans} for ip, c in self._clients.items() if c.banned_until > now),
                key=lambda b: b["until"], reverse=True)

    def unban(self, ip):
        with self._lock:
            c = self._clients.get(ip)
            if c is not None:
                c.banned_until = 0.0
                c.strikes = 0
                c.reset_budgets()
        if self.store is not None:
            try:
                self.store.drop_ban(ip)
            except Exception:
                logging.exception("Guard could not drop the ban")

    def sweep(self):
        now = self.clock()
        with self._lock:
            for ip in [ip for ip, c in self._clients.items() if c.seen < now - 7200 and c.banned_until < now]:
                del self._clients[ip]

    # -- crawlers ------------------------------------------------------------

    def _verified_crawler(self, ip):
        """Reverse DNS to a search engine's domain, confirmed by a forward lookup; cached a day."""
        now = self.clock()
        entry = self._crawlers.get(ip)
        if entry and entry[1] > now:
            return entry[0]
        verified = False
        try:
            host = socket.gethostbyaddr(ip)[0].lower().rstrip(".")
            if any(host.endswith(d) for d in CRAWLER_DOMAINS):
                verified = ip in {info[4][0] for info in socket.getaddrinfo(host, None)}
        except (socket.herror, socket.gaierror, OSError):
            verified = False
        self._crawlers[ip] = (verified, now + 24 * 3600)
        return verified


def client_ip(headers, remote_addr):
    """The visitor's address behind Cloudflare, a tunnel or nginx."""
    for name in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        value = (headers.get(name) or "").split(",")[0].strip()
        if value:
            return value
    return remote_addr or ""


def human_duration(seconds):
    seconds = int(seconds)
    if seconds < 120:
        return "a minute"
    if seconds < 3600:
        return "{0} minutes".format(seconds // 60)
    if seconds < 48 * 3600:
        return "{0} hours".format((seconds + 1800) // 3600)
    return "{0} days".format((seconds + 43200) // 86400)


MESSAGES = {
    "trap": "This address followed a link that is not shown to people, which is what scrapers do. It is blocked for a while.",
    "script": "This address reads pages with a script. Pages are for people; the JSON endpoints are for scripts, within their budget.",
    "banned": "This address kept going after being asked to slow down, and is blocked for a while.",
    "budget": "This address asked for more than a person reads. The budget refills within the minute or the hour.",
}


def html_answer(verdict):
    title = "Blocked" if verdict.reason in ("trap", "script", "banned") else "Slow down"
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{0}</title><meta name="robots" content="noindex"></head>'
            '<body style="font-family:system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem"><h1>{1}</h1><p>{2}</p>'
            '<p>Try again in about {3}.</p></body></html>').format(verdict.status, title, MESSAGES.get(verdict.reason, ""), human_duration(verdict.retry_after))
