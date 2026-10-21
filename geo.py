"""Country and city for a visitor's address, without an edge in front.

Cloudflare sends the country in a header; a station without it needs its own
table. The country ranges come from DB-IP's free "IP to Country Lite" (CC BY
4.0, a new file every month), kept in a small SQLite file of their own next
to the main database, so backups and the main file stay small. Cities need a
database DB-IP does not give away at a usable size; drop MaxMind's free
GeoLite2-City.mmdb in the data folder and cities appear. Lookups return a
country code or "City, CC" and nothing else; no address is stored.
Nothing in here imports the server.
"""

import gzip
import io
import ipaddress
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests

DBIP_URL = "https://download.db-ip.com/free/dbip-country-lite-{month}.csv.gz"
ATTRIBUTION = "IP geolocation by DB-IP (CC BY 4.0)"
USER_AGENT = "aircraft-noise/1.0 (+https://aircraftnoise.eu)"
REFRESH_AFTER_DAYS = 35
CITY_FILE = "GeoLite2-City.mmdb"

try:
    import maxminddb
except ImportError:  # the city database is optional
    maxminddb = None


def data_dir():
    base = os.environ.get("PN_DB_PATH")
    if base:
        return os.path.join(os.path.dirname(os.path.abspath(base)), "data") if os.path.basename(os.path.dirname(os.path.abspath(base))) != "data" else os.path.dirname(os.path.abspath(base))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def ip_key(ip):
    """16 bytes that sort like the address: IPv4 as an IPv4-mapped IPv6."""
    addr = ipaddress.ip_address(ip.strip())
    if addr.version == 4:
        addr = ipaddress.IPv6Address("::ffff:" + str(addr))
    return addr.packed


def parse_rows(text):
    """start,end,country lines of the DB-IP CSV into (start, end, cc) keys."""
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 3:
            continue
        cc = parts[2].strip().upper()
        if len(cc) != 2 or cc == "ZZ":
            continue
        try:
            yield ip_key(parts[0]), ip_key(parts[1]), cc
        except ValueError:
            continue


class Geo:
    def __init__(self, path=None, city_path=None, fetch=None):
        self.path = path or os.environ.get("GEO_DB_PATH") or os.path.join(data_dir(), "geo.db")
        self.city_path = city_path or os.path.join(data_dir(), CITY_FILE)
        self.fetch = fetch or self._download
        self._city = None
        self._city_mtime = None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS ranges (start BLOB PRIMARY KEY, end BLOB NOT NULL, cc TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    # -- lookups ---------------------------------------------------------------

    def country(self, ip):
        try:
            key = ip_key(ip)
        except (ValueError, AttributeError):
            return None
        try:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute("SELECT end, cc FROM ranges WHERE start <= ? ORDER BY start DESC LIMIT 1", (key,)).fetchone()
        except sqlite3.Error:
            return None
        if row and row[0] >= key:
            return row[1]
        return None

    def city(self, ip):
        reader = self._city_reader()
        if reader is None:
            return None
        try:
            rec = reader.get(ip.strip())
        except (ValueError, AttributeError):
            return None
        if not rec:
            return None
        name = ((rec.get("city") or {}).get("names") or {}).get("en")
        cc = (rec.get("country") or {}).get("iso_code")
        if not name:
            return None
        return "{0}, {1}".format(name, cc) if cc else name

    def _city_reader(self):
        if maxminddb is None or not os.path.exists(self.city_path):
            return None
        mtime = os.path.getmtime(self.city_path)
        if self._city is None or mtime != self._city_mtime:
            try:
                self._city = maxminddb.open_database(self.city_path)
                self._city_mtime = mtime
            except Exception:
                logging.exception("The city database could not be opened")
                return None
        return self._city

    # -- the table -------------------------------------------------------------

    def status(self):
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM ranges").fetchone()[0]
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        return {
            "rows": rows,
            "month": meta.get("month"),
            "updated_at": meta.get("updated_at"),
            "bytes": int(meta.get("bytes") or 0),
            "file_bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
            "city_db": self._city_reader() is not None,
            "city_path": self.city_path,
            "attribution": ATTRIBUTION,
        }

    def needs_refresh(self):
        st = self.status()
        if st["rows"] == 0 or not st["updated_at"]:
            return True
        try:
            updated = datetime.fromisoformat(st["updated_at"])
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - updated).days >= REFRESH_AFTER_DAYS

    def _download(self, month):
        response = requests.get(DBIP_URL.format(month=month), headers={"User-Agent": USER_AGENT}, timeout=120)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content

    def refresh(self):
        """Fetch this month's table, or last month's while the new one is not out yet."""
        now = datetime.now(timezone.utc)
        months = [now.strftime("%Y-%m"), (now.replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")]
        content, month = None, None
        for month in months:
            content = self.fetch(month)
            if content:
                break
        if not content:
            raise RuntimeError("DB-IP has no country table for {0}".format(" or ".join(months)))
        text = gzip.GzipFile(fileobj=io.BytesIO(content)).read().decode("utf-8", "replace")
        rows = list(parse_rows(text))
        if len(rows) < 1000:
            raise RuntimeError("The country table looks empty")
        tmp = self.path + ".tmp"
        if os.path.exists(tmp):
            os.unlink(tmp)
        with sqlite3.connect(tmp) as conn:
            conn.execute("CREATE TABLE ranges (start BLOB PRIMARY KEY, end BLOB NOT NULL, cc TEXT NOT NULL)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.executemany("INSERT OR REPLACE INTO ranges (start, end, cc) VALUES (?, ?, ?)", rows)
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [
                ("month", month), ("updated_at", now.replace(microsecond=0).isoformat()), ("bytes", str(len(content)))])
        os.replace(tmp, self.path)
        logging.info("Country table refreshed: %d ranges for %s", len(rows), month)
        return {"rows": len(rows), "month": month, "bytes": len(content)}
