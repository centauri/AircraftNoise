"""Community data: opt-in sharing and the public station list.

Each install can publish a blurred location and, from then on, its passes to
the Aircraft Noise data service. Exact sensor coordinates, the sensor id and
the raw noise samples stay on this server. The station list every install
shows on its community page comes from the same service.
"""

import hashlib
import json
import logging
import math
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import requests

import aircraft_types
import airlines
from database import db
from feeds import feed_by_id, infer_active

DEFAULT_AGGREGATOR = "https://data.aircraftnoise.eu/api/v1/stations"
BLUR_METRES = 200   # same privacy radius as the public map; the admin setting can change it
CACHE_SECONDS = 10 * 60
FAIL_SECONDS = 5 * 60
SYNC_SECONDS = 10 * 60          # record check and pass batch, near live
PASS_WINDOW_SECONDS = 2 * 3600  # every batch re-sends this window; late peak corrections get through
PASS_CATCHUP_SECONDS = 7 * 24 * 3600  # after an outage, everything since the last confirmed send, up to a week
PASS_BATCH = 500
USER_AGENT = "aircraft-noise-community/2.0 (+https://aircraftnoise.eu)"

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None, "failed_until": 0.0, "last_good": None}
_sync_started = False


def store_path():
    override = os.environ.get("COMMUNITY_DATA_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(__file__), "data", "monitors.json")


def aggregator_url(settings=None):
    """The station endpoint of the data service: GET for the list, POST for the record."""
    settings = settings or {}
    return canonical_url(settings.get("community_aggregator_url") or "") or DEFAULT_AGGREGATOR


def enabled(settings=None):
    """Sharing is on unless the owner switched it off: the point of a station is
    that its measurements count. A fresh install shares from its first pass."""
    settings = settings if settings is not None else db.get_all_admin_settings()
    return _truthy(settings.get("community_enabled", "true"))


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_url(url):
    """A stable public address, or an empty string when it is not a URL."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]  # www and the bare domain are one site, so one station id
    return urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))


def is_public_address(url):
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost":
        return False
    for suffix in (".local", ".lan", ".internal", ".home.arpa", ".localhost"):
        if host.endswith(suffix):
            return False
    if _is_ip(host):
        return _public_ip(host)
    return "." in host


def _is_ip(host):
    if host.count(".") == 3 and all(part.isdigit() and 0 <= int(part) <= 255 for part in host.split(".")):
        return True
    return ":" in host


def _public_ip(host):
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        a, b = int(parts[0]), int(parts[1])
        if a in (0, 10, 127) or a >= 224:
            return False
        if a == 169 and b == 254:
            return False
        if a == 172 and 16 <= b <= 31:
            return False
        if a == 192 and b == 168:
            return False
        if a == 100 and 64 <= b <= 127:
            return False
        return True
    return False


def monitor_id(url):
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:16]


def station_id(settings, url=None):
    """This install's station id, fixed for life. It is made once from the public
    address and then kept, so a changed address, a new domain or a move keeps the
    station, its claim and its history at the data service. An install that shared
    before this rule adopts the id it is already known by."""
    settings = settings or {}
    fixed = str(settings.get("community_station_id") or "").strip()
    if fixed:
        return fixed
    adopted = str(settings.get("community_last_id") or "").strip()
    if not adopted:
        url = canonical_url(url or settings.get("community_public_url"))
        # Without a public address the id is simply drawn once; it is kept for life either way.
        adopted = monitor_id(url) if url else secrets.token_hex(8)
    db.set_admin_setting("community_station_id", adopted)
    settings["community_station_id"] = adopted
    return adopted


def anonymize(lat, lon, station_id, radius_m=BLUR_METRES):
    """Shift a point by a stable offset inside radius_m. Same id, same shift."""
    max_lat = radius_m / 111320
    cos_lat = math.cos(math.radians(lat))
    max_lon = radius_m / (111320 * cos_lat) if cos_lat > 0.01 else max_lat
    digest = hashlib.sha256(("planenoise-blur|" + station_id).encode()).hexdigest()
    u1 = int(digest[:8], 16) / 0xFFFFFFFF
    u2 = int(digest[8:16], 16) / 0xFFFFFFFF
    angle = u1 * 2 * math.pi
    distance = 0.33 + 0.67 * math.sqrt(u2)
    return (
        round(lat + distance * max_lat * math.cos(angle), 5),
        round(lon + distance * max_lon * math.sin(angle), 5),
    )


def _feed_label(settings):
    active = infer_active(settings or {})
    feed = feed_by_id(active.get("noise"))
    name = (feed or {}).get("name") or "Noise monitor"
    return str(name)[:80]


def _country_code(settings):
    code = str((settings or {}).get("site_country") or "").strip().upper()
    if len(code) == 2 and code.isalpha():
        return code
    return ""


def build_record(settings):
    """The public record, or (None, reason). Sensor id is never copied in."""
    settings = settings or {}
    url = canonical_url(settings.get("community_public_url"))
    try:
        lat = float(settings.get("sensor_lat"))
        lon = float(settings.get("sensor_lon"))
    except (TypeError, ValueError):
        return None, "missing_location"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, "missing_location"
    name = str(settings.get("site_place_name") or "").strip()
    if not name or "<" in name or ">" in name or len(name) > 80:
        return None, "missing_name"
    station = station_id(settings, url)
    try:
        blur_m = max(50, min(int(float(settings.get("privacy_jitter_m") or BLUR_METRES)), 5000))
    except (TypeError, ValueError):
        blur_m = BLUR_METRES
    blurred_lat, blurred_lon = anonymize(lat, lon, station, radius_m=blur_m)
    public = bool(url) and is_public_address(url)
    record = {
        "id": station,
        "name": name,
        "latitude": blurred_lat,
        "longitude": blurred_lon,
        "country_code": _country_code(settings),
        "url": url if public else None,
        "local_only": not public,
        "feed": _feed_label(settings),
        "timezone": str(settings.get("site_timezone") or "UTC").strip() or "UTC",
        "updated_at": _now_iso(),
    }
    if not record["country_code"]:
        record.pop("country_code")
    return record, None


def payload_hash(record):
    stable = {key: value for key, value in record.items() if key != "updated_at"}
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _empty_document():
    return {"monitors": [], "last_updated": None}


def _read_local():
    path = store_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return _empty_document()
    if not isinstance(data, dict) or not isinstance(data.get("monitors"), list):
        return _empty_document()
    return data


def _write_local(document):
    path = store_path()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


def _public_monitor(raw):
    if not isinstance(raw, dict):
        return None
    station = str(raw.get("id") or "")
    if len(station) != 16 or any(ch not in "0123456789abcdef" for ch in station):
        return None
    name = str(raw.get("name") or "").strip()
    if not name or "<" in name or ">" in name or len(name) > 80:
        return None
    try:
        lat = round(float(raw.get("latitude")), 5)
        lon = round(float(raw.get("longitude")), 5)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    url = canonical_url(raw.get("url"))
    local_only = bool(raw.get("local_only")) or (url != "" and not is_public_address(url))
    if local_only:
        url = ""
    feed = str(raw.get("feed") or "").strip()
    if "<" in feed or ">" in feed:
        feed = ""
    monitor = {
        "id": station,
        "name": name,
        "latitude": lat,
        "longitude": lon,
        "url": url or None,
        "local_only": local_only or not url,
        "feed": feed[:80],
        "updated_at": str(raw.get("updated_at") or "")[:40],
    }
    if isinstance(raw.get("passes_7d"), (int, float)):
        monitor["passes_7d"] = int(raw["passes_7d"])
    country = str(raw.get("country_code") or "").strip().upper()
    if len(country) == 2 and country.isalpha():
        monitor["country_code"] = country
    return monitor


def _clean_document(data):
    monitors = []
    seen = set()
    for raw in data.get("stations") or data.get("monitors") or []:
        monitor = _public_monitor(raw)
        if not monitor or monitor["id"] in seen:
            continue
        seen.add(monitor["id"])
        monitors.append(monitor)
    updated = data.get("last_updated")
    return {"monitors": monitors, "last_updated": updated if isinstance(updated, str) else None}


def _remote_reads_enabled():
    flag = os.environ.get("COMMUNITY_SKIP_REMOTE", "").strip().lower()
    return flag not in ("1", "true", "yes")


def _read_remote():
    """The public station list from the data service."""
    if not _remote_reads_enabled():
        return None
    try:
        settings = db.get_all_admin_settings()
    except Exception:
        settings = {}
    url = aggregator_url(settings)
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
    except requests.RequestException as exc:
        logging.info("Community list read failed: %s", exc)
        return None
    if response.status_code != 200:
        logging.info("Community list HTTP %s from %s", response.status_code, url)
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return _clean_document(data)


def _combine(remote, local):
    """Remote wins per id. A monitor saved here still shows until the shared list has it."""
    by_id = {item["id"]: item for item in remote["monitors"]}
    for item in local["monitors"]:
        by_id.setdefault(item["id"], item)
    monitors = sorted(by_id.values(), key=lambda item: ((item.get("country_code") or "ZZ"), item.get("name") or ""))
    return {
        "monitors": monitors,
        "last_updated": remote.get("last_updated") or local.get("last_updated"),
    }


def load_monitors():
    """Public list. The data service first, then the last good copy, then the local file."""
    now = time.time()
    with _lock:
        cached = _cache["data"]
        if cached is not None and now - _cache["at"] < CACHE_SECONDS:
            return cached
        if now < _cache["failed_until"] and _cache["last_good"] is not None:
            return _cache["last_good"]
    remote = _read_remote()
    with _lock:
        local = _clean_document(_read_local())
        if remote is None:
            _cache["failed_until"] = time.time() + FAIL_SECONDS
            chosen = _cache["last_good"] if _cache["last_good"] is not None else local
        else:
            chosen = _combine(remote, local)
            _cache["failed_until"] = 0
        _cache["data"] = chosen
        _cache["at"] = time.time()
        _cache["last_good"] = chosen
        return chosen


def _invalidate_cache():
    _cache["at"] = 0
    _cache["data"] = None
    _cache["last_good"] = None
    _cache["failed_until"] = 0


def _merge(document, record):
    monitors = [item for item in document["monitors"] if item.get("id") != record["id"]]
    url = record.get("url")
    if url:
        monitors = [item for item in monitors if item.get("url") != url]
    monitors.append(record)
    monitors.sort(key=lambda item: (item.get("country_code") or "ZZ", item.get("name") or ""))
    return {"monitors": monitors, "last_updated": _now_iso()}


def _drop(document, station_id):
    monitors = [item for item in document["monitors"] if item.get("id") != station_id]
    return {"monitors": monitors, "last_updated": _now_iso()}


def merge_local(record):
    cleaned = _public_monitor(record)
    if not cleaned:
        return None
    with _lock:
        document = _merge(_clean_document(_read_local()), cleaned)
        _write_local(document)
        _invalidate_cache()
    return cleaned


def remove_local(station_id):
    with _lock:
        document = _drop(_clean_document(_read_local()), station_id)
        _write_local(document)
        _invalidate_cache()
        return document


def station_key():
    """This install's key at the data service, made once and kept with the other secrets."""
    key = db.get_admin_setting("api_secret.community_station_key", "") or ""
    if not key:
        key = secrets.token_hex(24)
        db.set_admin_setting("api_secret.community_station_key", key)
    return key


def _headers(api_key=None):
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json", "X-Station-Key": station_key()}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def post_remote(url, body, api_key=None):
    """Send one record or removal to the data service."""
    target = canonical_url(url)
    if not target or not _remote_reads_enabled():
        return "skipped"
    try:
        response = requests.post(target, json=body, headers=_headers(api_key), timeout=10)
    except requests.RequestException as exc:
        logging.info("Community post failed: %s", exc)
        return "failed"
    if response.ok:
        if isinstance(body, dict) and "monitor" in body:
            try:
                answer = response.json() or {}
            except Exception:
                answer = {}
            if "verified" in answer:
                db.set_admin_setting("community_verified", "true" if answer.get("verified") else "false")
        return "sent"
    if response.status_code == 403:
        logging.warning("Community: this station id is registered with another key at %s", target)
        return "rejected"
    logging.info("Community HTTP %s", response.status_code)
    return "failed"


def _remember(record):
    db.set_admin_setting("community_last_id", record["id"])
    db.set_admin_setting("community_last_hash", payload_hash(record))
    db.set_admin_setting("community_last_sent", record["updated_at"])


def share_now():
    settings = db.get_all_admin_settings()
    record, reason = build_record(settings)
    if not record:
        return {"ok": False, "error": reason}
    previous = str(settings.get("community_last_id") or "")
    if previous and previous != record["id"]:
        remove_local(previous)
        post_remote(aggregator_url(settings), {"action": "remove", "monitor_id": previous})
    stored = merge_local(record)
    aggregator = post_remote(aggregator_url(settings), {"monitor": stored})
    _remember(stored)
    if aggregator == "sent":
        _invalidate_cache()
    return {
        "ok": True,
        "preview": stored,
        "aggregator": aggregator,
        "message": _share_message(aggregator),
    }


def _share_message(aggregator):
    if aggregator == "sent":
        if db.get_admin_setting("community_verified", "") == "false":
            return "This station was sent and its passes follow within ten minutes. It joins the public map and totals after a week of steady reports, or at once when you claim it below."
        return "This station was sent. It is on the shared map now; its passes follow within ten minutes."
    if aggregator == "rejected":
        return "Saved on this server. The data service knows this address under another key; contact aircraftnoise.eu to claim it."
    if aggregator == "failed":
        return "Saved on this server. The data service did not accept it yet; check the address."
    return "Saved on this server. No data service address is set."


def pass_payload(row):
    """One pass as the data service takes it: no positions, no sensor id."""
    ts = float(row.get("timestamp") or 0)
    bucket = row.get("bucket_5m")
    if bucket is None:
        bucket = int(ts // 300)
    slant_m = None
    if row.get("peak_slant_km") is not None:
        slant_m = int(round(float(row["peak_slant_km"]) * 1000))
    elif row.get("distance_km") is not None:
        ground = float(row["distance_km"])
        height = float(row.get("altitude_m") or 0) / 1000.0
        slant_m = int(round(math.sqrt(ground * ground + height * height) * 1000))
    # The registry typecode names the exact variant (E175, not the E170 family of
    # the free-text model), so it wins when it is there.
    typecode = str(row.get("typecode") or "").upper()
    family = (aircraft_types.normalise_type(None, typecode) if typecode else None) \
        or aircraft_types.normalise_type(row.get("manufacturer"), row.get("model")) or ""
    return {
        "uid": "{0}:{1}".format(str(row.get("icao24") or "").lower(), int(bucket)),
        "ts": ts,
        "type_family": family,
        "typecode": typecode,
        "operator": str(row.get("operator") or "")[:80],
        "operator_code": airlines.designator(row.get("callsign")) or "",
        "route_kind": row.get("route_kind") or "",
        "airport": row.get("route_airport") or "",
        "runway": row.get("route_runway") or "",
        "route_label": row.get("route_label") or "",
        "spl_db": row.get("spl_db"),
        "spl_max_db": row.get("spl_max_db"),
        "slant_m": slant_m,
        "in_zone": True,
    }


def claim_station(code):
    """Link this station to the owner's account at the data service, proving the
    station key. The code comes from the account page there."""
    settings = db.get_all_admin_settings()
    station = str(settings.get("community_last_id") or "")
    if not enabled(settings) or not station:
        return {"ok": False, "error": "not_shared", "message": "Share the station first; then claim it."}
    code = str(code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "no_code", "message": "Enter the claim code from your account page."}
    url = "{0}/{1}/claim".format(aggregator_url(settings).rstrip("/"), station)
    try:
        response = requests.post(url, json={"code": code}, headers=_headers(), timeout=15)
    except requests.RequestException as exc:
        return {"ok": False, "error": "failed", "message": "The data service could not be reached: {0}".format(exc)}
    body = {}
    try:
        body = response.json()
    except ValueError:
        pass
    if response.ok:
        db.set_admin_setting("community_claimed_at", _now_iso())
        return {"ok": True, "message": "Claimed. The station is linked to your account at the data service."}
    return {"ok": False, "error": "http_{0}".format(response.status_code), "message": body.get("error") or "The data service refused the claim."}


def set_station_key(key):
    """Replace the station key, after a rotation on the account page."""
    key = str(key or "").strip()
    if len(key) < 32 or len(key) > 128 or not all(0x21 <= ord(ch) <= 0x7E for ch in key):
        return False
    db.set_admin_setting("api_secret.community_station_key", key)
    return True


def passes_url(settings, station_id):
    return "{0}/{1}/passes".format(aggregator_url(settings).rstrip("/"), station_id)


def passes_since(settings, now=None):
    """Where the next batch starts: two hours before the last confirmed send, so an
    outage of hours or days is caught up when the service is back, capped at a week.
    Without a confirmed send, the last two hours."""
    now = time.time() if now is None else now
    try:
        acked = float(settings.get("community_passes_acked_ts") or 0)
    except (TypeError, ValueError):
        acked = 0.0
    since = (acked - PASS_WINDOW_SECONDS) if acked > 0 else (now - PASS_WINDOW_SECONDS)
    return max(since, now - PASS_CATCHUP_SECONDS)


def share_passes(settings=None, force=False):
    """Send the in-zone passes since the last confirmed send, with two hours of
    overlap. Idempotent at the service, so the overlap costs nothing, brings late
    peak corrections along, and an outage loses nothing up to a week."""
    settings = settings or db.get_all_admin_settings()
    if not enabled(settings):
        return {"ok": False, "error": "disabled"}
    if not force and not _truthy(settings.get("community_share_passes", "true")):
        return {"ok": False, "error": "passes_off"}
    station = str(settings.get("community_last_id") or "")
    if not station:
        return {"ok": False, "error": "no_station"}
    if not _remote_reads_enabled():
        return {"ok": False, "error": "offline"}
    selected_at = time.time()
    rows = db.get_passes_for_sharing(passes_since(settings, selected_at))
    payload = [pass_payload(r) for r in rows if r.get("icao24")]
    sent = 0
    for start in range(0, len(payload), PASS_BATCH):
        chunk = payload[start:start + PASS_BATCH]
        try:
            response = requests.post(passes_url(settings, station), json={"passes": chunk}, headers=_headers(), timeout=20)
        except requests.RequestException as exc:
            logging.info("Community passes post failed: %s", exc)
            db.set_admin_setting("community_passes_last_error", str(exc)[:200])
            return {"ok": False, "error": "failed", "sent": sent}
        if not response.ok:
            logging.info("Community passes HTTP %s", response.status_code)
            db.set_admin_setting("community_passes_last_error", "HTTP {0}".format(response.status_code))
            return {"ok": False, "error": "http_{0}".format(response.status_code), "sent": sent}
        sent += len(chunk)
    db.set_admin_setting("community_passes_acked_ts", str(selected_at))
    db.set_admin_setting("community_passes_last_sent", _now_iso())
    db.set_admin_setting("community_passes_last_count", str(sent))
    db.set_admin_setting("community_passes_last_error", "")
    return {"ok": True, "sent": sent}


# ---- history ---------------------------------------------------------------
# The regular loop sends the last week. "Send full history" walks every older
# in-zone pass from the first one forward, a week at a time, and remembers how
# far it got, so it can be stopped and resumed. The service is idempotent on
# the pass id, so repeating a window costs nothing.

# Set by the server: a function returning the zone membership test, so passes
# recorded before the zone flag existed can be judged before they are sent.
zone_test = None

BACKFILL_WINDOW_SECONDS = 7 * 24 * 3600
BACKFILL_PAUSE_SECONDS = 1.5  # the service allows 60 ingest calls a minute per address
_backfill_lock = threading.Lock()
_backfill_thread = None
_backfill_stop = threading.Event()


def backfill_status(settings=None):
    settings = settings or db.get_all_admin_settings()
    state = str(settings.get("community_backfill_state") or "")
    running = _backfill_thread is not None and _backfill_thread.is_alive()
    if state == "running" and not running:
        state = "stopped"
    out = {
        "state": state,
        "sent": int(settings.get("community_backfill_sent") or 0),
        "stored": int(settings.get("community_backfill_stored") or 0),
        "skipped": int(settings.get("community_backfill_skipped") or 0),
        "total": int(settings.get("community_backfill_total") or 0),
        "reached": settings.get("community_backfill_reached") or "",
        "error": settings.get("community_backfill_error") or "",
    }
    # What is there to send: everything in the zone older than the regular window.
    try:
        end = time.time() - PASS_CATCHUP_SECONDS
        first = db.first_pass_ts()
        out["first"] = datetime.fromtimestamp(first, timezone.utc).strftime("%Y-%m-%d") if first else ""
        out["available"] = db.count_passes_for_sharing(first, end) if first and first < end else 0
        out["unflagged"] = db.count_unflagged_passes()
    except Exception:
        logging.exception("Community history overview failed")
    return out


def backfill_start():
    """Start, or resume, sending the whole history in the background."""
    global _backfill_thread
    settings = db.get_all_admin_settings()
    if not enabled(settings):
        return {"ok": False, "error": "disabled"}
    if not str(settings.get("community_last_id") or ""):
        return {"ok": False, "error": "no_station", "message": "Share the station first; the history follows the same id."}
    if not _remote_reads_enabled():
        return {"ok": False, "error": "offline"}
    with _backfill_lock:
        if _backfill_thread is not None and _backfill_thread.is_alive():
            return {"ok": True, "message": "Already sending.", "backfill": backfill_status()}
        _backfill_stop.clear()
        _backfill_thread = threading.Thread(target=_backfill_run, name="community-backfill", daemon=True)
        _backfill_thread.start()
    return {"ok": True, "message": "Sending the history in the background.", "backfill": backfill_status()}


def backfill_stop():
    _backfill_stop.set()
    return {"ok": True}


def _backfill_run():
    try:
        settings = db.get_all_admin_settings()
        station = str(settings.get("community_last_id") or "")
        if zone_test is not None and db.count_unflagged_passes() > 0:
            # Older installs only flagged new passes; judge the rest against the zone first.
            db.set_admin_setting("community_backfill_state", "running")
            flagged = db.recompute_zone_flags(zone_test())
            logging.info("Community history: judged %s stored passes against the zone", flagged)
        first = db.first_pass_ts()
        end = time.time() - PASS_CATCHUP_SECONDS  # the regular loop owns everything after this
        if first is None or first >= end:
            db.set_admin_setting("community_backfill_state", "done")
            db.set_admin_setting("community_backfill_error", "")
            return
        try:
            reached = float(settings.get("community_backfill_reached_ts") or 0)
        except (TypeError, ValueError):
            reached = 0.0
        start = max(first, reached) if reached > first else first
        if str(settings.get("community_backfill_state") or "") == "done" and reached >= end:
            start = first  # a second run resends everything; harmless and sometimes wanted
        total = db.count_passes_for_sharing(start, end)
        db.set_admin_setting("community_backfill_state", "running")
        db.set_admin_setting("community_backfill_total", str(total))
        db.set_admin_setting("community_backfill_sent", "0")
        db.set_admin_setting("community_backfill_error", "")
        sent = stored = skipped = 0
        db.set_admin_setting("community_backfill_stored", "0")
        db.set_admin_setting("community_backfill_skipped", "0")
        window = start
        while window < end and not _backfill_stop.is_set():
            until = min(window + BACKFILL_WINDOW_SECONDS, end)
            rows = db.get_passes_for_sharing(window, until)
            payload = [pass_payload(r) for r in rows if r.get("icao24")]
            for i in range(0, len(payload), PASS_BATCH):
                if _backfill_stop.is_set():
                    break
                chunk = payload[i:i + PASS_BATCH]
                response = requests.post(passes_url(settings, station), json={"passes": chunk}, headers=_headers(), timeout=30)
                if response.status_code == 429:
                    time.sleep(float(response.headers.get("Retry-After") or 30))
                    response = requests.post(passes_url(settings, station), json={"passes": chunk}, headers=_headers(), timeout=30)
                if not response.ok:
                    raise RuntimeError("HTTP {0}".format(response.status_code))
                sent += len(chunk)
                try:
                    answer = response.json() or {}
                except ValueError:
                    answer = {}
                stored += int(answer.get("stored") or 0)
                skipped += int(answer.get("skipped") or 0)
                db.set_admin_setting("community_backfill_sent", str(sent))
                db.set_admin_setting("community_backfill_stored", str(stored))
                db.set_admin_setting("community_backfill_skipped", str(skipped))
                time.sleep(BACKFILL_PAUSE_SECONDS)
            else:
                db.set_admin_setting("community_backfill_reached_ts", str(until))
                db.set_admin_setting("community_backfill_reached", datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d"))
                window = until
                continue
            break
        db.set_admin_setting("community_backfill_state", "done" if window >= end else "stopped")
    except Exception as exc:
        logging.exception("Community history send failed")
        db.set_admin_setting("community_backfill_state", "failed")
        db.set_admin_setting("community_backfill_error", str(exc)[:200])


def unpublish():
    """Take the station off the shared list. The id stays, so sharing again later
    continues the same station instead of starting a new one."""
    settings = db.get_all_admin_settings()
    station = str(settings.get("community_last_id") or "")
    if not station:
        record, _reason = build_record(settings)
        station = record["id"] if record else ""
    if station:
        remove_local(station)
        post_remote(aggregator_url(settings), {"action": "remove", "monitor_id": station})
    db.set_admin_setting("community_last_id", "")
    db.set_admin_setting("community_last_hash", "")
    db.set_admin_setting("community_last_sent", "")
    return {"ok": True, "message": "This station was removed from the shared list; no more passes are sent."}


def admin_status():
    settings = db.get_all_admin_settings()
    record, reason = build_record(settings)
    return {
        "enabled": enabled(settings),
        "share_passes": _truthy(settings.get("community_share_passes", "true")),
        "public_url": settings.get("community_public_url") or "",
        "aggregator_url": aggregator_url(settings),
        "station_key_set": bool(db.get_admin_setting("api_secret.community_station_key", "")),
        "station_id": settings.get("community_station_id") or settings.get("community_last_id") or "",
        "last_sent": settings.get("community_last_sent") or "",
        "backfill": backfill_status(settings),
        "passes_last_sent": settings.get("community_passes_last_sent") or "",
        "passes_last_count": int(settings.get("community_passes_last_count") or 0),
        "passes_last_error": settings.get("community_passes_last_error") or "",
        "claimed_at": settings.get("community_claimed_at") or "",
        "verified": settings.get("community_verified") or "",
        "preview": record,
        "preview_error": reason,
    }


def save_settings(body):
    body = body or {}
    public_url = canonical_url(body.get("public_url"))
    if body.get("public_url") and not public_url:
        return {"ok": False, "error": "invalid_url"}, 400
    aggregator = canonical_url(body.get("aggregator_url") or DEFAULT_AGGREGATOR)
    if not aggregator:
        return {"ok": False, "error": "invalid_aggregator"}, 400
    enabled = bool(body.get("enabled"))
    db.set_admin_setting("community_public_url", public_url)
    db.set_admin_setting("community_aggregator_url", aggregator)
    db.set_admin_setting("community_enabled", "true" if enabled else "false")
    db.set_admin_setting("community_share_passes", "true" if body.get("share_passes", True) else "false")
    if str(body.get("station_key") or "").strip() and not set_station_key(body.get("station_key")):
        return {"ok": False, "error": "invalid_station_key"}, 400
    if enabled:
        result = share_now()
        if result.get("ok") and result.get("aggregator") == "sent":
            result["passes"] = share_passes()
        status = 200 if result.get("ok") else 400
        return result, status
    result = unpublish()
    return result, 200


def sync_if_due():
    try:
        settings = db.get_all_admin_settings()
    except Exception:
        logging.exception("Community sync could not read settings")
        return
    if not enabled(settings):
        return
    record, _reason = build_record(settings)
    if not record:
        return
    unchanged = payload_hash(record) == str(settings.get("community_last_hash") or "")
    if not unchanged or _report_is_stale(settings.get("community_last_sent")):
        share_now()
        settings = db.get_all_admin_settings()
    try:
        share_passes(settings)
    except Exception:
        logging.exception("Community passes could not be sent")


REPORT_EVERY_SECONDS = 24 * 3600


def _report_is_stale(last_sent):
    """The data service drops stations that stay silent for weeks, so an unchanged
    record is still re-sent once a day."""
    try:
        sent = datetime.fromisoformat(str(last_sent or "").replace("Z", "+00:00"))
    except ValueError:
        return True
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - sent).total_seconds() >= REPORT_EVERY_SECONDS


def start_sync_thread():
    global _sync_started
    if _sync_started:
        return
    _sync_started = True

    def loop():
        while True:
            time.sleep(SYNC_SECONDS)
            try:
                sync_if_due()
            except Exception:
                logging.exception("Community sync failed")

    threading.Thread(target=loop, name="community_sync", daemon=True).start()
