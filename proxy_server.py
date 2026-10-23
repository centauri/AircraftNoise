#!/usr/bin/env python3
import os, time, json, logging, base64, re, hashlib, hmac, secrets
import math
import sqlite3
import threading
from typing import Union, Optional, Tuple
from html import escape
from flask.sessions import SecureCookieSessionInterface
from flask import Flask, request, jsonify, Response, send_file, session, redirect, g
from flask_cors import CORS
import requests
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from database import db
import notifications as _notifications_module
_notifications_module.settings_getter = db.get_admin_setting
import aircraft_db
import aircraft_types
import airlines
import notifications
import routes
from feeds import (
    FEEDS,
    feed_by_id,
    infer_active,
    materialize_configs,
    multi_members,
    public_field,
    tracking_key_for,
    usage_template,
    coerce_field,
)
import analytics
import community
import geo
import guard
from datetime import timedelta, datetime, timezone
import time
import pytz
from tls_config import TLSConfig, redirect_to_https

# Timezone helper functions
def _place_timezone_name():
    """IANA zone saved for this installation, with the original Netherlands default."""
    name = db.get_admin_setting("site_timezone", "Europe/Amsterdam") or "Europe/Amsterdam"
    if name not in pytz.all_timezones_set:
        return "Europe/Amsterdam"
    return name

def public_station_notes():
    """Titled notes an admin wrote for the public page. Bad stored text is ignored."""
    raw = db.get_admin_setting("station_notes", "[]") or "[]"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    notes = _clean_station_notes(parsed)
    return notes or []


def _clean_station_notes(raw):
    """A short list of plain-text sections. None means the payload is not acceptable."""
    if not isinstance(raw, list) or len(raw) > 12:
        return None
    notes = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        title = _plain_note(item.get("title"), 80)
        body = _plain_note(item.get("body"), 1500)
        if title is None or body is None:
            return None
        if not title and not body:
            continue
        if not title:
            return None
        notes.append({"title": title, "body": body})
    return notes


def _plain_note(value, limit):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    text = text.strip()
    if "<" in text or ">" in text or len(text) > limit:
        return None
    return text


def public_place():
    """Place, flag, and clock zone shown to visitors. Unset keys stay on the Netherlands."""
    place = (db.get_admin_setting("site_place_name", "Netherlands") or "Netherlands").strip()[:80]
    country = (db.get_admin_setting("site_country", "nl") or "nl").strip().lower()
    flag = os.path.join(os.path.dirname(__file__), "static", "flags", "4x3", f"{country}.svg")
    if not re.fullmatch(r"[a-z]{2}", country) or not os.path.isfile(flag):
        country = "nl"
    return {
        "place_name": place or "Netherlands",
        "country": country,
        "timezone": _place_timezone_name(),
    }

def get_sensor_timezone():
    """Timezone used for this installation's clock and day boundaries."""
    return pytz.timezone(_place_timezone_name())

def get_sensor_timezone_offset():
    """Get current sensor timezone offset in hours from UTC"""
    sensor_tz = get_sensor_timezone()
    now = datetime.now(sensor_tz)
    return now.utcoffset().total_seconds() / 3600

app = Flask(__name__)
def _session_secret():
    """SECRET_KEY from the environment, else a key generated once and kept in the database.

    A fixed default would let anyone forge an admin session cookie.
    """
    configured = os.getenv('SECRET_KEY', '').strip()
    if configured:
        return configured
    try:
        stored = (db.get_admin_setting('session_secret', '') or '').strip()
        if len(stored) >= 32:
            return stored
        generated = secrets.token_hex(32)
        db.set_admin_setting('session_secret', generated)
        return generated
    except Exception:
        logging.warning("Session secret could not be stored; sessions end with this process")
        return secrets.token_hex(32)


app.secret_key = _session_secret()
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)  # Keep logged in for 30 days
# The session cookie is marked Secure when the visit came over HTTPS, directly or
# through a proxy or tunnel that says so in X-Forwarded-Proto. That is the
# default, "auto": a plain http://nas:8787 on the LAN works out of the box and a
# site behind TLS keeps the flag. SESSION_SECURE=true or false pins it.
_SESSION_SECURE = os.getenv('SESSION_SECURE', 'auto').strip().lower()
app.config['SESSION_COOKIE_SECURE'] = _SESSION_SECURE == 'true'


def _request_is_https():
    if request.is_secure:
        return True
    proto = (request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
    if proto == 'https':
        return True
    visitor = request.headers.get('CF-Visitor') or ''
    return '"scheme":"https"' in visitor.replace(' ', '')


class _AutoSecureSessionInterface(SecureCookieSessionInterface):
    """Secure flag per request when SESSION_SECURE is auto."""

    def get_cookie_secure(self, app):
        if _SESSION_SECURE in ('true', 'false'):
            return _SESSION_SECURE == 'true'
        try:
            return _request_is_https()
        except RuntimeError:  # outside a request
            return False


app.session_interface = _AutoSecureSessionInterface()
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Protect session from JS access
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Same-site navigation keeps cookie
app.config['SESSION_COOKIE_NAME'] = 'plane_noise_session'
app.config['SESSION_COOKIE_DOMAIN'] = None  # Use current domain
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['SESSION_COOKIE_MAX_AGE'] = 30 * 24 * 60 * 60  # 30 days in seconds
app.config['SESSION_REFRESH_EACH_REQUEST'] = True  # Refresh session on each request
CORS(app)

ADMIN_IDLE_SECONDS = 12 * 60 * 60


def hash_password(password: str) -> str:
    """Salted PBKDF2-SHA256. Stored as pbkdf2$<iterations>$<salt hex>$<digest hex>."""
    salt = os.urandom(16)
    iterations = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iterations, salt_hex, digest_hex = stored.split("$", 3)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
            return hmac.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False
    legacy = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(legacy, stored)


# Login throttle: the guard exempts /auth/ so a locked-out admin can always
# reach the login, which means this is the only brake on password guessing.
# After LOGIN_FAILURES wrong tries from one address, or against one account,
# within LOGIN_WINDOW seconds, the login answers 429 until the window passes.
LOGIN_FAILURES = 5
LOGIN_WINDOW = 15 * 60
_login_failures = {}  # key -> [timestamps]
_login_lock = threading.Lock()


def _login_blocked(keys):
    now = time.time()
    with _login_lock:
        for key in keys:
            stamps = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW]
            _login_failures[key] = stamps
            if len(stamps) >= LOGIN_FAILURES:
                return int(LOGIN_WINDOW - (now - stamps[0])) + 1
        return 0


def _login_failed(keys):
    now = time.time()
    with _login_lock:
        for key in keys:
            _login_failures.setdefault(key, []).append(now)
        if len(_login_failures) > 10000:  # never grows without bound
            _login_failures.clear()


def _login_succeeded(keys):
    with _login_lock:
        for key in keys:
            _login_failures.pop(key, None)


def _admin_session_ok() -> bool:
    if not session.get('admin_logged_in'):
        return False
    last = session.get('last_activity') or session.get('login_time') or 0
    try:
        idle = time.time() - float(last)
    except (TypeError, ValueError):
        idle = ADMIN_IDLE_SECONDS + 1
    if idle > ADMIN_IDLE_SECONDS:
        session.clear()
        return False
    return True


def require_admin_auth(f):
    """Require a live admin session. API and debug calls get 401 instead of an HTML redirect."""
    def decorated_function(*args, **kwargs):
        if not _admin_session_ok():
            if request.path.startswith('/api/') or request.path.startswith('/debug/') or request.method not in ('GET', 'HEAD'):
                return jsonify({"error": "auth_required"}), 401
            return redirect('/admin/login')
        session.permanent = True
        session['last_activity'] = time.time()
        session.modified = True
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# force=True: the database module logs while it is imported, which gives the root
# logger an implicit WARNING-level handler first; without force this call would do
# nothing and every INFO line, including the admin log page, would be dropped.
logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    force=True,
)

_SECRET_LOG_WORDS = ("password", "secret", "token", "authorization", "api_key", "apikey")


class _SecretLogFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        if any(word in message.lower() for word in _SECRET_LOG_WORDS):
            record.msg = "redacted"
            record.args = ()
        return True


class _RingLogHandler(logging.Handler):
    """Keep a short in-memory log for the admin logs page. Secrets are not stored."""

    def __init__(self, capacity=400):
        super().__init__()
        self.capacity = capacity
        self.records = []
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return
        message = re.sub(r"\x1b\[[0-9;]*m", "", message)
        text = f"{record.name} {message}".lower()
        if re.search(r"\b(login|session|auth)\b", text):
            source = "auth"
        elif re.search(r"\b(noise|sensor)\b", text):
            source = "sensor"
        elif re.search(r"\b(aircraft|opensky|icao)\b", text):
            source = "aircraft"
        elif re.search(r"\b(api|request)\b", text):
            source = "api"
        else:
            source = "system"
        entry = {
            "timestamp": datetime.utcfromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname.lower(),
            "source": source,
            "message": message[:500],
        }
        with self._lock:
            self.records.append(entry)
            extra = len(self.records) - self.capacity
            if extra > 0:
                del self.records[:extra]

    def snapshot(self):
        with self._lock:
            return list(self.records)


RING_LOGS = _RingLogHandler()
logging.getLogger().addFilter(_SecretLogFilter())
logging.getLogger().addHandler(RING_LOGS)

ALLOWED_NETLOCS = {
    "maps.sensor.community",
    "sensor.community",
    "api-rrd.madavi.de",
    "api-rrd.madavi.de:3000",
    "data.sensor.community",
    "grafana.sensor.community",
}

# OpenSky OAuth2 token endpoint (client-credentials / Keycloak)
OAUTH_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

# Simple JSONL log file (rotate by size in MB)
LOG_FILE = os.getenv("PN_LOG_FILE", os.path.join(os.getcwd(), "events.jsonl"))
LOG_MAX_MB = float(os.getenv("PN_LOG_MAX_MB", "20"))
# The microphone position comes from the admin settings (set in the setup
# walkthrough). These are only the fallback before setup, so 0,0: "not set".
SENSOR_LAT = float(os.getenv("PN_SENSOR_LAT", "0"))
SENSOR_LON = float(os.getenv("PN_SENSOR_LON", "0"))

def _ensure_log_path():
    try:
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        if not os.path.exists(LOG_FILE):
            # Touch the file so users can see it exists
            with open(LOG_FILE, "a", encoding="utf-8"):
                pass
        logging.info("Log file ready: %s (max %.1f MB)", LOG_FILE, LOG_MAX_MB)
    except Exception as e:
        logging.warning("Failed preparing log path %s: %s", LOG_FILE, e)

_ensure_log_path()

_SECRET_PREFIX = "api_secret."


EXTRA_SECRET_NAMES = ["mail_password"]  # secrets that belong to no feed


def known_secret_names():
    names = list(EXTRA_SECRET_NAMES)
    for feed in FEEDS:
        for secret in feed.get("secrets") or []:
            key = secret["key"]
            if key not in names:
                names.append(key)
    return names


def _secret_setting_name(name):
    return _SECRET_PREFIX + name


def _read_legacy_secret_file():
    path = os.path.join(os.getcwd(), "credentials.json")
    found = {}
    if not os.path.exists(path):
        return found
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        logging.warning("Could not read the old credentials file during import")
        return found
    if not isinstance(data, dict):
        return found
    sources = [data]
    nested = data.get("opensky")
    if isinstance(nested, dict):
        sources.append(nested)
    allowed = set(known_secret_names())
    for source in sources:
        for key, value in source.items():
            if key not in allowed or isinstance(value, (dict, list)):
                continue
            text = "" if value is None else str(value).strip()
            if text and key not in found:
                found[key] = text
    return found


def import_legacy_secrets():
    """Copy credentials.json into admin settings once, then ignore that file."""
    if db.get_admin_setting("api_secrets_imported") == "1":
        return
    legacy = _read_legacy_secret_file()
    for name in known_secret_names():
        current = db.get_admin_setting(_secret_setting_name(name), "") or ""
        if str(current).strip() or name not in legacy:
            continue
        db.set_admin_setting(_secret_setting_name(name), legacy[name])
    db.set_admin_setting("api_secrets_imported", "1")


def load_api_secrets() -> dict:
    """Credentials saved from the API admin page. Nothing else supplies them."""
    import_legacy_secrets()
    creds = {}
    for name in known_secret_names():
        value = db.get_admin_setting(_secret_setting_name(name), "") or ""
        text = str(value).strip()
        if text:
            creds[name] = text
    return creds


def store_api_secret(name, value):
    if name not in known_secret_names():
        return False
    text = "" if value is None else str(value).strip()
    db.set_admin_setting(_secret_setting_name(name), text)
    if text:
        CREDS[name] = text
    else:
        CREDS.pop(name, None)
    return True


CREDS = load_api_secrets()

_token_cache = {"access_token": None, "expires_at": 0}

# Simple cache for API responses to reduce rate limiting
_api_cache = {}

# One station reading for every aircraft in a five-minute window, including a failed call.
STATION_SNAPSHOT_SECONDS = 300
_station_snapshot_cache = {"at": None, "surface": None, "hourly": None}

# Rate limiting tracking
_rate_limit_trackers = {}
_feed_failed = {}

# API usage tracking. Rows come from the feed catalog.
_api_usage_stats = usage_template()

def check_rate_limit(api_name: str, rate_limit: int) -> bool:
    """Check if we can make a request without exceeding rate limit"""
    now = time.time()
    
    if api_name not in _rate_limit_trackers:
        _rate_limit_trackers[api_name] = []
    
    # Remove requests older than 1 minute
    _rate_limit_trackers[api_name] = [
        req_time for req_time in _rate_limit_trackers[api_name] 
        if now - req_time < 60
    ]
    
    # Check if we're under the limit
    if len(_rate_limit_trackers[api_name]) < rate_limit:
        _rate_limit_trackers[api_name].append(now)
        return True
    
    return False

def track_api_usage(api_name: str, headers):
    """Track API usage from response headers for any API"""
    try:
        tracking_key = tracking_key_for(api_name)
        if not tracking_key or tracking_key not in _api_usage_stats:
            return
        
        # Common rate limit headers across APIs
        remaining = headers.get('X-RateLimit-Remaining') or headers.get('X-Rate-Limit-Remaining')
        limit = headers.get('X-RateLimit-Limit') or headers.get('X-Rate-Limit-Limit')
        reset_time = headers.get('X-RateLimit-Reset') or headers.get('X-Rate-Limit-Reset')
        
        # Update in-memory stats
        if remaining is not None:
            _api_usage_stats[tracking_key]['remaining_requests'] = int(remaining)
        
        if limit is not None:
            _api_usage_stats[tracking_key]['daily_limit'] = int(limit)
        
        if reset_time is not None:
            _api_usage_stats[tracking_key]['last_reset'] = int(reset_time)
        
        # Increment daily request counter in memory
        _api_usage_stats[tracking_key]['daily_requests'] += 1
        
        # Track in database for persistence across restarts
        db.track_api_usage(
            api_name=tracking_key,
            daily_requests=1,
            daily_limit=int(limit) if limit is not None else _api_usage_stats[tracking_key]['daily_limit'],
            remaining_requests=int(remaining) if remaining is not None else _api_usage_stats[tracking_key]['remaining_requests'],
            last_reset_timestamp=int(reset_time) if reset_time is not None else _api_usage_stats[tracking_key]['last_reset'],
            rate_limit_per_minute=_api_usage_stats[tracking_key]['rate_limit_per_minute'],
            pricing=_api_usage_stats[tracking_key]['pricing'],
            reset_type=_api_usage_stats[tracking_key]['reset_type']
        )
        
        feed = feed_by_id(api_name)
        if feed and feed.get('log_usage'):
            remaining_str = _api_usage_stats[tracking_key]['remaining_requests'] or 'unknown'
            logging.info(f"{_api_usage_stats[tracking_key]['name']} API usage: {_api_usage_stats[tracking_key]['daily_requests']} requests today, {remaining_str} remaining")
        
    except Exception as e:
        logging.warning(f"Failed to track {api_name} usage: {e}")

def track_opensky_usage(headers):
    """Legacy function - now calls generic track_api_usage"""
    track_api_usage('aircraft', headers)

def get_api_usage_stats():
    """Get current API usage statistics from database"""
    try:
        # Load from database first
        db_stats = db.get_api_usage_stats()
        
        # Merge with in-memory stats, prioritizing database values
        merged_stats = _api_usage_stats.copy()
        for api_name, db_data in db_stats.items():
            if api_name in merged_stats:
                merged_stats[api_name].update(db_data)
            else:
                merged_stats[api_name] = db_data
        
        return merged_stats
    except Exception as e:
        logging.warning(f"Failed to load API usage from database, using in-memory stats: {e}")
        return _api_usage_stats.copy()

def load_api_usage_from_database():
    """Load API usage stats from database on startup"""
    try:
        # Clean up old records first
        db.cleanup_old_api_usage(days_to_keep=30)
        
        db_stats = db.get_api_usage_stats()
        
        # Update in-memory stats with database values
        for api_name, db_data in db_stats.items():
            if api_name in _api_usage_stats:
                _api_usage_stats[api_name].update(db_data)
                logging.info(f"Loaded {api_name} usage: {db_data.get('daily_requests', 0)} requests today")
        
        logging.info("API usage stats loaded from database")
    except Exception as e:
        logging.warning(f"Failed to load API usage from database: {e}")

# Default API settings (will be overridden by database values)
# Aircraft APIs
DEFAULT_AIRCRAFT_RATE_LIMIT = 30
DEFAULT_AIRCRAFT_CACHE_TIMEOUT = 120
DEFAULT_AIRCRAFT_TIMEOUT = 20

# Noise APIs
DEFAULT_NOISE_RATE_LIMIT = 60
DEFAULT_NOISE_CACHE_TIMEOUT = 30
DEFAULT_NOISE_TIMEOUT = 30

# FlightAware specific (free tier limits)
DEFAULT_FLIGHTAWARE_RATE_LIMIT = 10
DEFAULT_FLIGHTAWARE_CACHE_TIMEOUT = 300
DEFAULT_FLIGHTAWARE_TIMEOUT = 15

# RIVM specific (no auth required)
DEFAULT_RIVM_RATE_LIMIT = 120
DEFAULT_RIVM_CACHE_TIMEOUT = 300
DEFAULT_RIVM_TIMEOUT = 30

_cache_timeout = DEFAULT_AIRCRAFT_CACHE_TIMEOUT  # Cache for 2 minutes

# Per-API default poll intervals (seconds)
DEFAULT_AIRCRAFT_POLL_INTERVAL = 300
# A jet crosses the 8 km circle in about 90 to 120 s. 30 s is about 2,880
# OpenSky calls a day, inside the 4,000 registered allowance.
OPENSKY_MIN_POLL_INTERVAL = 30
OPENSKY_DAY_FLOOR = 20          # never faster than this, even at peak hours
OPENSKY_DAILY_ALLOWANCE = 4000


def _parse_clock(text, fallback):
    """'HH:MM' to minutes after midnight."""
    try:
        hours, minutes = str(text).strip().split(':')
        return (int(hours) % 24) * 60 + int(minutes) % 60
    except (TypeError, ValueError, AttributeError):
        return fallback


def opensky_schedule(settings=None):
    """The quiet window and the two OpenSky intervals, with the daily call estimate.

    The allowance is 4,000 calls a day. Polling slower in the quiet hours and
    faster the rest of the day puts the calls where the traffic is.
    """
    settings = settings if settings is not None else db.get_all_admin_settings()
    start = _parse_clock(settings.get('poll_quiet_start', '01:00'), 60)
    end = _parse_clock(settings.get('poll_quiet_end', '05:00'), 300)
    try:
        day_interval = max(OPENSKY_DAY_FLOOR, int(float(settings.get('poll_day_interval_s') or OPENSKY_MIN_POLL_INTERVAL)))
    except (TypeError, ValueError):
        day_interval = OPENSKY_MIN_POLL_INTERVAL
    try:
        quiet_interval = max(day_interval, int(float(settings.get('poll_quiet_interval_s') or 60)))
    except (TypeError, ValueError):
        quiet_interval = max(day_interval, 60)
    quiet_minutes = (end - start) % 1440
    estimate = int((1440 - quiet_minutes) * 60 / day_interval + quiet_minutes * 60 / quiet_interval)
    return {
        'quiet_start': f"{start // 60:02d}:{start % 60:02d}",
        'quiet_end': f"{end // 60:02d}:{end % 60:02d}",
        'day_interval_s': day_interval,
        'quiet_interval_s': quiet_interval,
        'calls_per_day': estimate,
        'allowance': OPENSKY_DAILY_ALLOWANCE,
        'within_allowance': estimate <= OPENSKY_DAILY_ALLOWANCE,
    }


def _in_quiet_hours(schedule, now=None):
    from datetime import datetime as _dt
    zone = pytz.timezone(_place_timezone_name())
    local = _dt.now(zone) if now is None else now
    minute = local.hour * 60 + local.minute
    start = _parse_clock(schedule['quiet_start'], 60)
    end = _parse_clock(schedule['quiet_end'], 300)
    if start == end:
        return False
    return start <= minute < end if start < end else (minute >= start or minute < end)


def opensky_interval_now(settings=None):
    """The OpenSky poll interval for this moment, by the quiet-hours schedule."""
    schedule = opensky_schedule(settings)
    if not schedule['within_allowance']:
        # Over budget: fall back to the flat floor rather than burn the allowance.
        return OPENSKY_MIN_POLL_INTERVAL
    return schedule['quiet_interval_s'] if _in_quiet_hours(schedule) else schedule['day_interval_s']
DEFAULT_NOISE_POLL_INTERVAL = 300

# Runtime configs are built from feeds.py. Adding a feed starts there.
API_CONFIGS = materialize_configs({})
AIRCRAFT_FETCHERS = {}
NOISE_FETCHERS = {}

def get_cached_response(cache_key: str):
    """Get cached response if still valid"""
    if cache_key in _api_cache:
        data, timestamp = _api_cache[cache_key]
        if time.time() - timestamp < _cache_timeout:
            return data
        else:
            # Remove expired cache entry
            del _api_cache[cache_key]
    return None

def set_cached_response(cache_key: str, data):
    """Cache a response with current timestamp"""
    _api_cache[cache_key] = (data, time.time())

def add_api_config(api_name: str, config: dict):
    """Add a new API configuration to the system"""
    API_CONFIGS[api_name] = config
    logging.info(f"Added API configuration for: {api_name}")

def update_api_config(api_name: str, **kwargs):
    """Update specific settings for an existing API"""
    if api_name in API_CONFIGS:
        API_CONFIGS[api_name].update(kwargs)
        logging.info(f"Updated API configuration for: {api_name}")
        
        # Also save to database for persistence
        try:
            for key, value in kwargs.items():
                db.set_admin_setting(f"{api_name}_{key}", str(value))
            logging.info(f"Saved {api_name} configuration to database")
        except Exception as e:
            logging.error(f"Failed to save {api_name} configuration to database: {e}")
    else:
        raise ValueError(f"API {api_name} not found")

def get_api_config(api_name: str):
    """Get configuration for a specific API"""
    return API_CONFIGS.get(api_name)

def load_api_configs_from_database():
    """Rebuild runtime configs from the feed catalog and saved settings."""
    try:
        db_settings = db.get_all_admin_settings()
        built = materialize_configs(db_settings)
        API_CONFIGS.clear()
        API_CONFIGS.update(built)
        logging.info("API configurations loaded. Available APIs: %s", list(API_CONFIGS.keys()))
    except Exception as e:
        logging.error("Failed to load API configurations from database: %s", e)

def reload_api_configs():
    """Reload API configurations from database (called after admin changes)"""
    try:
        logging.info("Reloading API configurations from database...")
        load_api_configs_from_database()
        logging.info("API configurations reloaded successfully")
    except Exception as e:
        logging.error(f"Failed to reload API configurations: {e}")

def get_noise_sensor_id():
    """Get the configured sensor ID for noise monitoring"""
    noise_config = get_api_config('noise')
    return noise_config.get('sensor_id', '69616')

def get_rivm_noise_data():
    """Get noise data from RIVM API using location-based search"""
    try:
        noise_config = get_api_config('noise_rivm')
        if not noise_config:
            return None
        
        # Get location from config
        default_lat, default_lon = sensor_position()
        lat = noise_config.get('latitude', default_lat)
        lon = noise_config.get('longitude', default_lon)
        thing_id = noise_config.get('thing_id', '')
        
        if thing_id:
            # Use specific thing ID
            response = make_api_request(
                api_name='noise_rivm',
                endpoint='observations',
                params={'datastream_id': thing_id},
                cache_key=f"rivm_thing_{thing_id}"
            )
        else:
            # Search for nearby locations
            response = make_api_request(
                api_name='noise_rivm',
                endpoint='nearby_locations',
                params={'lat': lat, 'lon': lon, 'distance': 0.01},
                cache_key=f"rivm_nearby_{lat}_{lon}"
            )
        
        return response
    except Exception as e:
        logging.error(f"Failed to get RIVM noise data: {e}")
        return None

def calculate_bbox(lat, lon, radius_km):
    """Calculate bounding box for given lat/lon and radius"""
    import math
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * max(math.cos(math.radians(lat)), 1e-6))
    
    return {
        'lamin': lat - lat_delta,
        'lomin': lon - lon_delta,
        'lamax': lat + lat_delta,
        'lomax': lon + lon_delta
    }

def get_aircraft_data_multi_api():
    """Ask enabled aircraft feeds in priority order. The first feed that returns aircraft wins."""
    try:
        db_settings = db.get_all_admin_settings()
        if str(db_settings.get('multi_api_multi_api', '')).lower() != 'true':
            return []

        members = [row for row in multi_members(db_settings) if row['enabled']]
        if not members:
            logging.warning("No aircraft feeds enabled for multi-feed polling")
            return []

        aircraft_data = []
        for member in members:
            feed = feed_by_id(member['id'])
            fetcher = AIRCRAFT_FETCHERS.get(feed['fetcher']) if feed else None
            if not fetcher:
                logging.warning("No fetcher registered for %s", member['id'])
                continue
            if member['id'] == 'aircraft_flightaware' and _flightaware_enrichment_only():
                continue
            try:
                data = fetcher()
            except Exception as exc:
                logging.warning("Feed %s failed: %s", member['id'], exc)
                _mark_feed(member['id'], True)
                continue
            if data:
                aircraft_data.extend(data)
                logging.info("Got %s aircraft from %s (priority %s)", len(data), member['id'], member['priority'])
                break
            if not _feed_failed.get(member['id']):
                logging.info("No aircraft from %s (priority %s)", member['id'], member['priority'])
                return []
            logging.info("No data from %s (priority %s)", member['id'], member['priority'])

        if aircraft_data:
            merged_data = merge_aircraft_data(aircraft_data)
            logging.info("Multi-feed: %s unique aircraft", len(merged_data))
            return merged_data
        logging.warning("Multi-feed: no data from any feed")
        return []
    except Exception as exc:
        logging.error("Failed to get multi-feed aircraft data: %s", exc)
        return []

def get_opensky_aircraft_data():
    """Get aircraft data from OpenSky Network"""
    try:
        # Get current settings for bounding box
        settings = db.get_all_admin_settings()
        radius_km = float(settings.get('radius_km', 8.0))
        lat, lon = sensor_position()
        
        # Calculate bounding box
        bbox = calculate_bbox(lat, lon, radius_km)
        
        # Temporarily restore OpenSky configuration for this call
        original_config = API_CONFIGS['aircraft'].copy()
        API_CONFIGS['aircraft'].update({
            'name': 'OpenSky Network',
            'base_url': 'https://opensky-network.org/api',
            'endpoints': {
                'states_all': '/states/all?lamin={lamin}&lomin={lomin}&lamax={lamax}&lomax={lomax}',
                'flight_track': '/tracks/all'
            },
            'auth_required': True
        })
        
        response = make_api_request(
            api_name='aircraft',
            endpoint='states_all',
            params=bbox,
            cache_key=f'opensky_states_all_{bbox["lamin"]}_{bbox["lomin"]}_{bbox["lamax"]}_{bbox["lomax"]}'
        )
        
        # Restore original configuration
        API_CONFIGS['aircraft'] = original_config
        
        if response.status_code == 200:
            opensky_data = response.json()
            states_list = (opensky_data.get('states') or [])
            aircraft_data = []
            
            for state in states_list:
                if len(state) >= 17:  # Ensure we have all required fields
                    aircraft_data.append({
                        'icao24': state[0],
                        'callsign': state[1].strip() if state[1] else '',
                        'origin_country': state[2],
                        'time_position': state[3],
                        'last_contact': state[4],
                        'longitude': state[5],
                        'latitude': state[6],
                        'baro_altitude': state[7],
                        'on_ground': state[8],
                        'velocity': state[9],
                        'true_track': state[10],
                        'vertical_rate': state[11],
                        'sensors': state[12],
                        'geo_altitude': state[13],
                        'squawk': state[14],
                        'spi': state[15],
                        'position_source': state[16],
                        'source': 'opensky'
                    })
            
            return aircraft_data
        elif response.status_code == 429:
            logging.info("OpenSky Network API rate limited")
            return []
        else:
            logging.warning(f"OpenSky Network API returned status {response.status_code}")
            return []
            
    except Exception as e:
        logging.warning(f"OpenSky Network API failed: {e}")
        return []

# One result set is about $0.05. Three calls a day stay near the $5 monthly credit.
FLIGHTAWARE_POSITION_CALLS_PER_DAY = 3
FLIGHTAWARE_ENRICHMENT_CALLS_PER_DAY = 30
# Thirty lookups spread across the day, so a busy hour cannot spend them all.
FLIGHTAWARE_ENRICHMENT_GAP_S = 86400 // FLIGHTAWARE_ENRICHMENT_CALLS_PER_DAY
_fa_enrich_cache = {}
_fa_enrich_next_at = 0.0


def _sensor_bbox(radius_km):
    """Box around the sensor used by the aircraft pollers. Distances use the same point."""
    lat, lon = sensor_position()
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * max(math.cos(math.radians(lat)), 1e-6))
    return (
        lat - lat_delta,
        lon - lon_delta,
        lat + lat_delta,
        lon + lon_delta,
    )


def _mark_feed(feed_id, failed):
    _feed_failed[feed_id] = bool(failed)


def _flightaware_flag(key, default):
    raw = (get_api_config("aircraft_flightaware") or {}).get(key, default)
    if raw is None:
        return default
    return raw is True or str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _flightaware_spend_enabled():
    """Off keeps position searches inside the free monthly credit."""
    return _flightaware_flag("spend_beyond_credit", False)


def _flightaware_enrichment_only():
    """On keeps FlightAware to one cheap lookup per pass."""
    return _flightaware_flag("enrichment_only", True)


def _flightaware_used_today():
    try:
        stats = db.get_api_usage_stats()
        return int((stats.get("flightaware") or {}).get("daily_requests") or 0)
    except Exception:
        logging.warning("Could not read the FlightAware usage count")
        return 0


def get_flightaware_aircraft_data():
    """Airborne positions inside the sensor box. One page. The daily stop holds unless spending is on."""
    feed_id = "aircraft_flightaware"
    if _flightaware_enrichment_only():
        return []
    key = CREDS.get("flightaware_key")
    if not key:
        logging.warning("FlightAware API key not found in credentials")
        _mark_feed(feed_id, True)
        return []
    used = _flightaware_used_today()
    if not _flightaware_spend_enabled() and used >= FLIGHTAWARE_POSITION_CALLS_PER_DAY:
        logging.warning(
            "FlightAware position search paused after %s calls today",
            used,
        )
        _mark_feed(feed_id, True)
        return []
    try:
        settings = db.get_all_admin_settings()
        radius_km = float(settings.get("radius_km", 8.0))
        lamin, lomin, lamax, lomax = _sensor_bbox(radius_km)
        query = "{range lat %.5f %.5f} {range lon %.5f %.5f}" % (
            lamin, lamax, lomin, lomax,
        )
        base_url = (get_api_config(feed_id) or {}).get("base_url") or "https://aeroapi.flightaware.com/aeroapi"
        url = base_url.rstrip("/") + "/flights/search/positions"
        response = requests.get(
            url,
            headers={"User-Agent": "plane-noise-dashboard/1.0", "x-apikey": key, "Accept": "application/json"},
            params={"query": query, "max_pages": 1, "unique_flights": "true"},
            timeout=15,
        )
        track_api_usage("aircraft_flightaware", response.headers)
        if response.status_code != 200:
            logging.warning("FlightAware positions status=%s body=%s", response.status_code, response.text[:200])
            _mark_feed(feed_id, True)
            return []
        aircraft = _flightaware_aircraft(response.json() or {})
        _mark_feed(feed_id, False)
        return aircraft
    except Exception as exc:
        logging.warning("FlightAware positions failed: %s", exc)
        _mark_feed(feed_id, True)
        return []


def _flightaware_aircraft(body):
    """Positions from either a flight list or a position list. Search altitude is in hundreds of feet."""
    rows = body.get("flights") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows:
        rows = body.get("positions") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    aircraft = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        position = row.get("last_position") if isinstance(row.get("last_position"), dict) else row
        lat = position.get("latitude")
        lon = position.get("longitude")
        if lat is None or lon is None:
            continue
        ident = row.get("ident") or row.get("registration") or position.get("ident") or ""
        icao = row.get("hexid") or row.get("icao24") or row.get("registration") or row.get("fa_flight_id") or position.get("fa_flight_id") or ident
        icao = str(icao or "").strip().lower()
        if not icao:
            continue
        altitude_level = position.get("altitude")
        altitude_m = None
        if isinstance(altitude_level, (int, float)):
            # This endpoint reports altitude in hundreds of feet.
            altitude_m = float(altitude_level) * 100.0 * 0.3048
        aircraft.append({
            "icao24": icao,
            "callsign": str(ident).strip(),
            "longitude": lon,
            "latitude": lat,
            "geo_altitude": altitude_m,
            "baro_altitude": altitude_m,
            "velocity": position.get("groundspeed"),
            "true_track": position.get("heading"),
            "last_contact": _flightaware_timestamp(position.get("timestamp")),
            "source": "flightaware",
        })
    return aircraft


def _flightaware_timestamp(value):
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def _flightaware_identity(body):
    """Type, registration, and destination from one flight lookup."""
    flights = body.get("flights") if isinstance(body, dict) else None
    if not isinstance(flights, list):
        return None
    chosen = None
    for flight in flights:
        if isinstance(flight, dict) and flight.get("aircraft_type"):
            chosen = flight
            break
    if chosen is None and flights and isinstance(flights[0], dict):
        chosen = flights[0]
    if not isinstance(chosen, dict):
        return None
    dest = chosen.get("destination") if isinstance(chosen.get("destination"), dict) else {}
    destination = dest.get("name") or dest.get("city") or dest.get("code_iata") or dest.get("code")
    model = chosen.get("aircraft_type") or None
    registration = chosen.get("registration") or None
    operator = chosen.get("operator") or chosen.get("operator_iata") or None
    if not model and not destination and not registration:
        return None
    return {
        "model": model,
        "registration": registration,
        "operator": operator,
        "destination": destination or None,
    }


def _flightaware_enrich(callsign):
    """One flight lookup for a pass OpenSky already saw. Cached. No position search."""
    global _fa_enrich_next_at
    if not _flightaware_enrichment_only():
        return None
    ident = re.sub(r"[^A-Z0-9]", "", str(callsign or "").upper())
    if not re.fullmatch(r"[A-Z][A-Z0-9]{2,7}", ident):
        return None
    if ident in _fa_enrich_cache:
        return _fa_enrich_cache[ident]
    key = CREDS.get("flightaware_key")
    if not key or _flightaware_used_today() >= FLIGHTAWARE_ENRICHMENT_CALLS_PER_DAY:
        return None
    if time.time() < _fa_enrich_next_at:
        return None
    _fa_enrich_next_at = time.time() + FLIGHTAWARE_ENRICHMENT_GAP_S
    try:
        base_url = (get_api_config("aircraft_flightaware") or {}).get("base_url") or "https://aeroapi.flightaware.com/aeroapi"
        url = base_url.rstrip("/") + "/flights/" + quote(ident)
        response = requests.get(
            url,
            headers={"User-Agent": "plane-noise-dashboard/1.0", "x-apikey": key, "Accept": "application/json"},
            params={"max_pages": 1},
            timeout=15,
        )
        track_api_usage("aircraft_flightaware", response.headers)
        parsed = _flightaware_identity(response.json() or {}) if response.status_code == 200 else None
        _fa_enrich_cache[ident] = parsed
        return parsed
    except Exception as exc:
        logging.warning("FlightAware enrichment failed: %s", exc)
        return None


def _flight_identity(icao24, callsign, meta):
    """Make, model, and destination for a stored pass. FlightAware fills the blanks."""
    enriched = _flightaware_enrich(callsign)
    manufacturer = (meta or {}).get("manufacturerName") or (meta or {}).get("manufacturer")
    model = (meta or {}).get("model") or (meta or {}).get("type")
    registration = (meta or {}).get("registration")
    # Operator: the callsign's airline designator first (current and precise for scheduled
    # flights), then the registry's owner, then FlightAware.
    operator = airlines.operator_for_callsign(callsign, _airline_table()) or (meta or {}).get("operator")
    if enriched:
        model = model or enriched.get("model")
        registration = registration or enriched.get("registration")
        operator = operator or enriched.get("operator")
    destination = (enriched or {}).get("destination")
    if not destination:
        destination = get_airport_name(get_aircraft_destination(icao24, callsign))
    return {
        "manufacturer": manufacturer,
        "model": model,
        "registration": registration,
        "operator": operator,
        "destination": destination,
    }

def merge_aircraft_data(aircraft_list):
    """Merge aircraft data from multiple sources, prioritizing most recent/complete data"""
    try:
        # Group by ICAO24 code
        aircraft_dict = {}
        
        for aircraft in aircraft_list:
            icao24 = aircraft.get('icao24', '').strip()
            if not icao24:
                continue
                
            if icao24 not in aircraft_dict:
                aircraft_dict[icao24] = aircraft
            else:
                # Merge data, prioritizing more complete information
                existing = aircraft_dict[icao24]
                
                # Prefer data with position information
                if aircraft.get('latitude') and aircraft.get('longitude') and not existing.get('latitude'):
                    aircraft_dict[icao24] = aircraft
                # Prefer data with callsign
                elif aircraft.get('callsign') and not existing.get('callsign'):
                    aircraft_dict[icao24] = aircraft
                # Prefer more recent data (if we have timestamps)
                elif aircraft.get('last_contact') and existing.get('last_contact'):
                    if aircraft.get('last_contact', 0) > existing.get('last_contact', 0):
                        aircraft_dict[icao24] = aircraft
        
        return list(aircraft_dict.values())
        
    except Exception as e:
        logging.error(f"Failed to merge aircraft data: {e}")
        return aircraft_list

def make_api_request(api_name: str, endpoint: str, params: dict = None, cache_key: str = None):
    """
    Generic API request function with caching and rate limiting
    
    Args:
        api_name: Key from API_CONFIGS (e.g., 'noise', 'aircraft')
        endpoint: Endpoint key from the API config
        params: Parameters to substitute in the endpoint URL
        cache_key: Optional custom cache key, otherwise auto-generated
    
    Returns:
        requests.Response object or cached response
    """
    if api_name not in API_CONFIGS:
        raise ValueError(f"Unknown API: {api_name}")
    
    config = API_CONFIGS[api_name]

    if api_name == "weather":
        params = dict(params or {})
        params.setdefault("api_key", CREDS.get("openweather_key") or "")
    
    # Build URL
    if endpoint in config['endpoints']:
        url_template = config['endpoints'][endpoint]
        if params:
            endpoint_url = url_template.format(**params)
        else:
            endpoint_url = url_template
        
        # Combine base URL with endpoint
        if endpoint_url.startswith('http'):
            # Full URL provided
            url = endpoint_url
        else:
            # Relative URL, combine with base URL
            base_url = config['base_url'].rstrip('/')
            if endpoint_url.startswith('/'):
                # Endpoint starts with /, so just append directly
                url = f"{base_url}{endpoint_url}"
            else:
                # Endpoint doesn't start with /, add one
                url = f"{base_url}/{endpoint_url}"
        
    else:
        raise ValueError(f"Unknown endpoint '{endpoint}' for API '{api_name}'")

    # The catalog weather URL has no units field. Without this, main.temp is Kelvin.
    if api_name == "weather":
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["units"] = "metric"
        url = urlunparse(parsed._replace(query=urlencode(query)))
    
    # Generate cache key if not provided
    if not cache_key:
        cache_key = f"{api_name}_{endpoint}_{hash(str(params))}"
    
    # Check rate limit first
    rate_limit = config.get('rate_limit', 30)
    if not check_rate_limit(api_name, rate_limit):
        logging.warning(f"Rate limit exceeded for {api_name} API ({rate_limit} requests/minute)")
        # Return a mock response with rate limit error
        from unittest.mock import Mock
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.text = "Rate limit exceeded"
        return mock_response
    
    # Check cache first - use API-specific cache timeout
    cache_timeout = config.get('cache_timeout', _cache_timeout)
    # Do not cache live aircraft state polling to avoid stale passes
    bypass_cache = (api_name == 'aircraft' and endpoint == 'states_all')
    
    if not bypass_cache and cache_key in _api_cache:
        data, timestamp = _api_cache[cache_key]
        if time.time() - timestamp < cache_timeout:
            logging.info(f"Using cached {api_name} API data")
            return data
        else:
            # Remove expired cache entry
            del _api_cache[cache_key]
    
    # Prepare headers
    headers = config['headers'].copy()
    feed = feed_by_id(api_name)
    auth_kind = (feed or {}).get('auth')
    if config.get('auth_required', False) and auth_kind == 'opensky':
        auth_headers, mode = get_auth_headers()
        headers.update(auth_headers)
    elif config.get('auth_required', False) and auth_kind == 'flightaware':
        flightaware_key = CREDS.get('flightaware_key')
        if flightaware_key:
            headers['x-apikey'] = flightaware_key
        else:
            logging.warning("FlightAware API key not found in credentials")
    elif config.get('auth_required', False) and auth_kind:
        logging.warning("No auth handler registered for %s", auth_kind)
    
    # Make request
    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=config['timeout']
        )
        
        # Track API usage from response headers for all APIs
        if response.status_code == 200:
            track_api_usage(api_name, response.headers)
        
        # Cache successful responses
        if response.status_code == 200 and not bypass_cache:
            _api_cache[cache_key] = (response, time.time())
            logging.info(f"Cached {api_name} API data")
        
        return response
        
    except requests.RequestException as e:
        logging.error(f"API request failed for {api_name}: {e}")
        raise

def get_oauth_token(cid: str, csec: str) -> Optional[str]:
    """Get/refresh OAuth2 token via client credentials."""
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - 30 > now:
        return _token_cache["access_token"]
    try:
        basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        }
        data = {"grant_type": "client_credentials"}
        r = requests.post(OAUTH_TOKEN_URL, headers=headers, data=data, timeout=20)
        if r.status_code != 200:
            logging.error("OAuth token failed: %s %s", r.status_code, r.text[:200])
            return None
        j = r.json()
        _token_cache["access_token"] = j.get("access_token")
        _token_cache["expires_at"] = now + int(j.get("expires_in", 1800))
        logging.info("OAuth token OK; expires in %ss", int(j.get("expires_in", 1800)))
        return _token_cache["access_token"]
    except Exception as e:
        logging.exception("Fetching OAuth token failed")
        return None

def get_auth_headers() -> tuple[dict, str]:
    """Return (headers, mode) where mode is 'oauth2'|'basic'|'none'"""
    if CREDS.get("clientId") and CREDS.get("clientSecret"):
        tok = get_oauth_token(CREDS["clientId"], CREDS["clientSecret"])
        if tok:
            return ({"Authorization": f"Bearer {tok}"}, "oauth2")
    if CREDS.get("username") and CREDS.get("password"):
        # Basic handled by requests auth tuple at request time
        return ({}, "basic")
    return ({}, "none")

@app.get("/health")
@require_admin_auth
def health():
    hdrs, mode = get_auth_headers()
    # Measure a simple DB query for response time
    try:
        t0 = time.time()
        total_events = db.get_events_count()
        resp_ms = int((time.time() - t0) * 1000)
        connection_status = "Online"
    except Exception:
        total_events = 0
        resp_ms = 0
        connection_status = "Unknown"

    return jsonify({
        "ok": connection_status == "Online",
        "auth_mode": mode,
        "token": ("Authorization" in hdrs),
        "token_url": OAUTH_TOKEN_URL,
        "log_file": LOG_FILE,
        "log_exists": os.path.exists(LOG_FILE),
        "connection_status": connection_status,
        "response_time": resp_ms,
        "total_events": total_events,
    })

@app.get("/api/usage")
@require_admin_auth
def api_usage():
    """Get API usage statistics"""
    return jsonify(get_api_usage_stats())

@app.get("/api/admin/performance")
@require_admin_auth
def api_admin_performance():
    try:
        status = db.sqlite_status()
        return jsonify({
            "connection_status": "Online" if status["integrity"] == "ok" else "Check failed",
            "response_time": status["query_ms"],
            "integrity": status["integrity"],
            "journal_mode": status["journal_mode"],
            "events": status["events"],
            "page_count": status["page_count"],
            "freelist_count": status["freelist_count"],
            "index_count": status["index_count"],
            "indexes": status["indexes"],
            "file_bytes": status["file_bytes"],
        })
    except Exception:
        logging.exception("database status failed")
        return jsonify({"error": "perf_failed"}), 500

@app.get("/debug/noise")
@require_admin_auth
def debug_noise():
    """Debug endpoint to check noise data storage"""
    global latest_noise_data, noise_data_history
    return jsonify({
        "latest_noise_data": latest_noise_data,
        "noise_data_history_count": len(noise_data_history),
        "noise_data_history_sample": noise_data_history[:3] if noise_data_history else None
    })

@app.get("/api/noise/recent")
def api_noise_recent():
    """Return recent noise samples from in-memory buffer (no upstream calls)."""
    try:
        limit = max(1, min(int(request.args.get('limit', 300)), 5000))
    except Exception:
        limit = 300
    global noise_data_history
    data = noise_data_history[-limit:] if limit > 0 else noise_data_history
    return jsonify({
        "data": data,
        "count": len(data)
    })

@app.get("/debug/test-noise-correlation")
@require_admin_auth
def debug_test_noise_correlation():
    """Test noise correlation with current time"""
    current_time_ms = time.time() * 1000
    peak_noise = find_peak_noise_around_time(current_time_ms)
    return jsonify({
        "current_time_ms": current_time_ms,
        "current_time_seconds": current_time_ms / 1000,
        "peak_noise": peak_noise,
        "noise_data_available": len(noise_data_history) > 0,
        "debug": last_noise_pick_debug
    })

@app.get("/debug/test-noise-correlation-recent")
@require_admin_auth
def debug_test_noise_correlation_recent():
    """Test noise correlation with a recent noise data timestamp"""
    global noise_data_history
    if noise_data_history and len(noise_data_history) > 0:
        # Use the most recent noise data timestamp
        recent_timestamp = noise_data_history[-1]['timestamp']
        test_time_ms = recent_timestamp * 1000
        peak_noise = find_peak_noise_around_time(test_time_ms)
        return jsonify({
            "test_time_ms": test_time_ms,
            "test_time_seconds": test_time_ms / 1000,
            "peak_noise": peak_noise,
            "noise_data_available": len(noise_data_history) > 0,
            "recent_noise_sample": noise_data_history[-1] if noise_data_history else None
        })
    else:
        return jsonify({"error": "No noise data available"})

@app.get("/debug/test-noise-at")
@require_admin_auth
def debug_test_noise_at():
    """Test noise correlation at a provided time. Accepts time_local=YYYY-MM-DD HH:MM or time_utc=YYYY-MM-DD HH:MM"""
    try:
        time_local = (request.args.get('time_local') or '').strip()
        time_utc = (request.args.get('time_utc') or '').strip()
        from datetime import datetime, timedelta
        if time_local:
            tl = time_local.replace('T', ' ')
            dt_local = datetime.strptime(tl, '%Y-%m-%d %H:%M')
            from config import Config
            offset_h = get_sensor_timezone_offset()
            dt_utc = dt_local - timedelta(hours=offset_h)
        elif time_utc:
            tu = time_utc.replace('T', ' ')
            dt_utc = datetime.strptime(tu, '%Y-%m-%d %H:%M')
        else:
            return jsonify({"error": "provide time_local or time_utc"}), 400
        ts_ms = dt_utc.timestamp() * 1000.0
        val = find_peak_noise_around_time(ts_ms)
        return jsonify({
            "input_time_utc": dt_utc.strftime('%Y-%m-%d %H:%M'),
            "ts_ms": ts_ms,
            "peak_noise": val,
            "debug": last_noise_pick_debug,
            "noise_data_available": len(noise_data_history) > 0
        })
    except Exception as e:
        return jsonify({"error": "failed", "details": str(e)}), 500

# --- Admin utility: recalc recent noise for pass events ---
@app.post("/api/admin/recalc_recent_noise")
@require_admin_auth
def admin_recalc_recent_noise():
    try:
        limit = int(request.args.get('limit', '15'))
        # Fetch recent pass events
        recent_passes = db.get_recent_events(limit=limit, event_type='pass')
        updated = []
        for ev in recent_passes:
            try:
                ts = float(ev.get('timestamp') or 0)
                if not ts:
                    continue
                gated = None
                peak_ts = None
                if noise_association_allowed(ev.get('distance_km'), ev.get('altitude_m')):
                    point = closest_track_point(ev.get('icao24'))
                    target, slant = point if point else (ts, _row_slant(ev))
                    picked = claim_noise_peak(ev.get('icao24'), target, slant, band_s=60.0)
                    if picked:
                        gated, peak_ts = picked
                # Only update if we have a better value
                current = ev.get('spl_db')
                if gated is not None and (current is None or float(gated) > float(current)):
                    db.update_pass_metrics(ev['id'], spl_db=float(gated), spl_peak_ts=peak_ts, spl_max_db=noise_la_max_at(peak_ts))
                    updated.append({
                        'id': ev['id'],
                        'icao24': ev.get('icao24'),
                        'callsign': ev.get('callsign'),
                        'old': current,
                        'new': float(gated)
                    })
            except Exception as e:
                logging.warning(f"recalc_recent_noise failed for event {ev.get('id')}: {e}")
        return jsonify({
            'updated_count': len(updated),
            'updated': updated
        })
    except Exception as e:
        return jsonify({'error': 'recalc_failed', 'details': str(e)}), 500


# Targeted recalc for a specific aircraft (by callsign or icao24)
@app.post("/api/admin/recalc_noise_for")
@require_admin_auth
def admin_recalc_noise_for():
    try:
        callsign = (request.args.get('callsign') or '').strip().upper()
        icao24_param = (request.args.get('icao24') or '').strip().lower()
        limit = int(request.args.get('limit', '50'))
        time_local = (request.args.get('time_local') or '').strip()
        time_utc = (request.args.get('time_utc') or '').strip()
        skip_gate = (request.args.get('skip_gate') or '').strip().lower() in ('1','true','yes','y')

        recent_passes = db.get_recent_events(limit=limit, event_type='pass')
        # Filter by identifier
        targets = []
        for ev in recent_passes:
            cs = (ev.get('callsign') or '').strip().upper()
            i24 = (ev.get('icao24') or '').strip().lower()
            if (callsign and cs == callsign) or (icao24_param and i24 == icao24_param):
                targets.append(ev)

        if not targets:
            return jsonify({'updated_count': 0, 'updated': [], 'message': 'no matching recent pass events'})

        updated = []
        for ev in targets:
            try:
                i24 = (ev.get('icao24') or '').strip().lower()
                # Prefer closest_approach timestamp and geometry if available
                ca = db.get_closest_approach(i24) if i24 else None
                ts_ref = 0.0
                # 1) If an explicit time is provided, use it (convert local -> UTC)
                if time_local:
                    try:
                        from datetime import datetime, timedelta
                        # Accept both "YYYY-MM-DD HH:MM" and ISO "YYYY-MM-DDTHH:MM"
                        tl = time_local.replace('T', ' ')
                        dt_local = datetime.strptime(tl, '%Y-%m-%d %H:%M')
                        # Convert local (sensor timezone) to UTC
                        from config import Config
                        offset_h = get_sensor_timezone_offset()
                        dt_utc = dt_local - timedelta(hours=offset_h)
                        ts_ref = dt_utc.timestamp()
                    except Exception:
                        ts_ref = 0.0
                elif time_utc:
                    try:
                        from datetime import datetime
                        tu = time_utc.replace('T', ' ')
                        dt_utc = datetime.strptime(tu, '%Y-%m-%d %H:%M')
                        ts_ref = dt_utc.timestamp()
                    except Exception:
                        ts_ref = 0.0
                # 2) Otherwise, prefer closest_approach ts, then pass ts
                if not ts_ref:
                    ts_ref = float((ca or {}).get('timestamp') or ev.get('timestamp') or 0)
                if not ts_ref:
                    continue
                ts_ms = ts_ref * 1000.0
                picked = find_peak_noise_around_time(ts_ms, peak_band_s=60.0)

                # Choose distance/alt from closest_approach first, else pass
                distance_km = (ca or {}).get('distance_km')
                altitude_m = (ca or {}).get('altitude_m')
                if distance_km is None:
                    distance_km = ev.get('distance_km')
                if altitude_m is None:
                    altitude_m = ev.get('altitude_m')

                gated = None
                if picked is not None and (skip_gate or noise_association_allowed(distance_km, altitude_m)):
                    gated = picked

                current = ev.get('spl_db')
                if gated is not None and (current is None or float(gated) > float(current)):
                    db.update_pass_metrics(ev['id'], spl_db=float(gated))
                    updated.append({'id': ev['id'], 'icao24': i24, 'callsign': ev.get('callsign'), 'old': current, 'new': float(gated)})
            except Exception as e:
                logging.warning(f"recalc_noise_for failed for event {ev.get('id')}: {e}")

        return jsonify({'updated_count': len(updated), 'updated': updated})
    except Exception as e:
        return jsonify({'error': 'recalc_failed', 'details': str(e)}), 500


# InfluxDB test endpoint removed

# ---- OpenSky (bbox) ---------------------------------------------------------

def fetch_opensky_states(lamin: float, lomin: float, lamax: float, lomax: float, *, time_unix: Optional[int] = None):
    """Fetch aircraft states from OpenSky API using the new API system"""
    try:
        # Use the new API system
        params = {'lamin': lamin, 'lomin': lomin, 'lamax': lamax, 'lomax': lomax}
        if time_unix is not None:
            params['time'] = int(time_unix)
        response = make_api_request(
            api_name='aircraft',
            endpoint='states_all',
            params=params,
            cache_key=f"aircraft_states_{lamin}_{lomin}_{lamax}_{lomax}"
        )
        
        # Handle OAuth token refresh if needed
        if response.status_code == 401:
            _token_cache["access_token"] = None
            _token_cache["expires_at"] = 0
            # Retry once with fresh token
            if time_unix is not None:
                params['time'] = int(time_unix)
            response = make_api_request(
                api_name='aircraft',
                endpoint='states_all',
                params=params,
                cache_key=f"aircraft_states_{lamin}_{lomin}_{lamax}_{lomax}_retry"
            )
        
        return response
        
    except Exception as e:
        logging.error(f"Failed to fetch OpenSky states: {e}")
        # Return a mock response with error status
        from unittest.mock import Mock
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = str(e)
        return mock_response

@app.get("/opensky")
@require_admin_auth
def opensky():
    bbox = request.args.get("bbox", "").strip()
    time_unix = request.args.get("time")
    if not bbox:
        return jsonify({"error": "missing bbox"}), 400
    try:
        lamin, lomin, lamax, lomax = [float(x) for x in bbox.split(",")]
    except Exception:
        return jsonify({"error": "bad bbox format"}), 400
    r = fetch_opensky_states(lamin, lomin, lamax, lomax, time_unix=int(time_unix) if time_unix else None)
    if r.status_code != 200:
        return jsonify({"error": "upstream", "status": r.status_code, "body": r.text[:400]}), 502
    return Response(r.content, status=200, content_type=r.headers.get("Content-Type", "application/json"))

# ---- CSV passthrough (direct) ----------------------------------------------

@app.get("/fetch_csv")
@require_admin_auth
def fetch_csv():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400
    u = urlparse(url)
    if not u.scheme.startswith("http"):
        return jsonify({"error": "invalid scheme"}), 400
    host = u.netloc.split(":")[0].lower()
    if host not in ALLOWED_NETLOCS:
        return jsonify({"error": f"host not allowed: {host}"}), 403
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "plane-noise-dashboard/1.0"})
        if r.status_code != 200:
            return jsonify({"error": "upstream", "status": r.status_code, "body": r.text[:800]}), 502
        return Response(r.content, status=200, content_type=r.headers.get("Content-Type", "text/csv"))
    except requests.RequestException as e:
        return jsonify({"error": "request_exception", "details": str(e)}), 502

# ---- Grafana Inspect resolver -> CSV ---------------------------------------

@app.get("/grafana_csv")
@require_admin_auth
def grafana_csv():
    """
    Supports either:
      - /grafana_csv?csv_url=<direct CSV link>
      - /grafana_csv?inspect_url=<inspect page URL with inspectTab=data>
    """
    csv_url = request.args.get("csv_url")
    inspect_url = request.args.get("inspect_url")
    if not csv_url and not inspect_url:
        return jsonify({"error": "missing csv_url or inspect_url"}), 400

    try:
        if inspect_url and not csv_url:
            # Fetch the inspect HTML and try to extract a CSV link
            iu = urlparse(inspect_url)
            host = iu.netloc.split(":")[0].lower()
            if host not in ALLOWED_NETLOCS:
                return jsonify({"error": f"host not allowed: {host}"}), 403
            h = requests.get(inspect_url, timeout=30, headers={"User-Agent":"plane-noise-dashboard/1.0","Accept":"text/html,application/xhtml+xml","Referer":inspect_url})
            if h.status_code != 200:
                return jsonify({"error": "upstream", "status": h.status_code, "body": h.text[:400]}), 502
            html = h.text
            m = (
                re.search(r"href=['\"]([^'\"]+?format=csv[^'\"]*)['\"]", html, re.IGNORECASE)
                or re.search(r"href=['\"]([^'\"]+?\\.csv[^'\"]*)['\"]", html, re.IGNORECASE)
                or re.search(r"data-download[^>]*href=['\"]([^'\"]+)['\"]", html, re.IGNORECASE)
                or re.search(r"<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>[^<]*CSV[^<]*</a>", html, re.IGNORECASE)
            )
            if m:
                rel = m.group(1)
                if rel.startswith("http"):
                    csv_url = rel
                else:
                    scheme = iu.scheme
                    netloc = iu.netloc
                    if rel.startswith("/"):
                        csv_url = f"{scheme}://{netloc}{rel}"
                    else:
                        base = inspect_url.rsplit("/", 1)[0]
                        csv_url = f"{base}/{rel}"
            else:
                qs = dict(parse_qsl(iu.query, keep_blank_values=True))
                qs.pop("inspect", None)
                qs.pop("inspectTab", None)
                qs["format"] = "csv"
                qs["download"] = "true"
                new_query = urlencode(qs, doseq=True)
                # Prefer d-solo with panelId if available
                path = iu.path
                panel_id = qs.get("panelId")
                vp = qs.pop("viewPanel", None)
                if (not panel_id) and vp and isinstance(vp, str) and vp.startswith("panel-"):
                    try:
                        panel_id = str(int(vp.split("-", 1)[1]))
                        qs["panelId"] = panel_id
                    except Exception:
                        pass
                if "/grafana/d/" in path and "/grafana/d-solo/" not in path:
                    path = path.replace("/grafana/d/", "/grafana/d-solo/")
                new_query = urlencode(qs, doseq=True)
                csv_url = urlunparse((iu.scheme, iu.netloc, path, iu.params, new_query, iu.fragment))

        u = urlparse(csv_url)
        host2 = u.netloc.split(":")[0].lower()
        if host2 not in ALLOWED_NETLOCS:
            return jsonify({"error": f"host not allowed: {host2}"}), 403

        r = requests.get(csv_url, timeout=30, headers={"User-Agent":"plane-noise-dashboard/1.0","Accept":"text/csv,*/*;q=0.8","Referer":csv_url})
        if r.status_code != 200:
            return jsonify({"error": "upstream", "status": r.status_code, "body": r.text[:800]}), 502

        ct = (r.headers.get("Content-Type", "").lower())
        # If we accidentally received HTML, try to extract a CSV link from it and refetch
        if ("text/csv" not in ct) and ("<html" in r.text[:500].lower() or "text/html" in ct):
            html = r.text
            m = (
                re.search(r"href=['\"]([^'\"]+?format=csv[^'\"]*)['\"]", html, re.IGNORECASE)
                or re.search(r"href=['\"]([^'\"]+?\\.csv[^'\"]*)['\"]", html, re.IGNORECASE)
                or re.search(r"data-download[^>]*href=['\"]([^'\"]+)['\"]", html, re.IGNORECASE)
                or re.search(r"<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>[^<]*CSV[^<]*</a>", html, re.IGNORECASE)
            )
            if not m:
                uu = urlparse(csv_url)
                qs = dict(parse_qsl(uu.query, keep_blank_values=True))
                qs["format"] = "csv"
                qs["download"] = "true"
                csv_url2 = urlunparse((uu.scheme, uu.netloc, uu.path, uu.params, urlencode(qs, doseq=True), uu.fragment))
                r2 = requests.get(csv_url2, timeout=30, headers={"User-Agent":"plane-noise-dashboard/1.0","Accept":"text/csv,*/*;q=0.8","Referer":csv_url2})
                if r2.status_code != 200:
                    return jsonify({"error": "upstream", "status": r2.status_code, "body": r2.text[:800]}), 502
                ct2 = (r2.headers.get("Content-Type", "").lower())
                if ("text/csv" in ct2) or not ("<html" in r2.text[:500].lower() or "text/html" in ct2):
                    return Response(r2.content, status=200, content_type=r2.headers.get("Content-Type", "text/csv"))
                return jsonify({"error":"csv_link_not_found_in_inspect_html","note":"unable to derive CSV from page","hint":"Provide a direct csv_url (format=csv) via UI override"}), 502
            rel2 = m.group(1)
            if rel2.startswith("http"):
                csv_url2 = rel2
            else:
                bu = urlparse(csv_url)
                if rel2.startswith("/"):
                    csv_url2 = f"{bu.scheme}://{bu.netloc}{rel2}"
                else:
                    base = csv_url.rsplit("/", 1)[0]
                    csv_url2 = f"{base}/{rel2}"
            r2 = requests.get(csv_url2, timeout=30, headers={"User-Agent":"plane-noise-dashboard/1.0","Accept":"text/csv,*/*;q=0.8","Referer":csv_url2})
            if r2.status_code != 200:
                return jsonify({"error": "upstream", "status": r2.status_code, "body": r2.text[:800]}), 502
            return Response(r2.content, status=200, content_type=r2.headers.get("Content-Type", "text/csv"))

        return Response(r.content, status=200, content_type=r.headers.get("Content-Type", "text/csv"))
    except requests.RequestException as e:
        return jsonify({"error": "request_exception", "details": str(e)}), 502

# ---- Grafana JSON query passthrough -----------------------------------------

@app.post("/grafana_query")
@require_admin_auth
def grafana_query():
    """
    POST body JSON:
      { "query_url": "https://.../grafana/api/ds/query", "payload": { ... grafana query json ... } }
    We validate host against allowlist and relay the JSON to Grafana.
    """
    try:
        data = request.get_json(silent=True) or {}
        query_url = (data.get("query_url") or "").strip()
        payload = data.get("payload") or {}
        if not query_url:
            return jsonify({"error": "missing query_url"}), 400
        u = urlparse(query_url)
        host = u.netloc.split(":")[0].lower()
        if host not in ALLOWED_NETLOCS:
            return jsonify({"error": f"host not allowed: {host}"}), 403
        r = requests.post(
            query_url,
            json=payload,
            timeout=30,
            headers={
                "User-Agent": "plane-noise-dashboard/1.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": query_url,
            },
        )
        if r.status_code != 200:
            return jsonify({"error": "upstream", "status": r.status_code, "body": r.text[:800]}), 502
        return Response(r.content, status=200, content_type=r.headers.get("Content-Type", "application/json"))
    except requests.RequestException as e:
        return jsonify({"error": "request_exception", "details": str(e)}), 502

# ---- Aircraft metadata passthrough (OpenSky / other) ------------------------

def enhance_aircraft_metadata(meta):
    """Enhance aircraft metadata with additional information and image URLs"""
    enhanced = meta.copy()
    
    # Add image URL based on aircraft type
    manufacturer = (meta.get("manufacturerName") or meta.get("manufacturer") or "").lower()
    model = (meta.get("model") or meta.get("type") or "").lower()
    
    # Generate image URL based on aircraft type
    image_url = generate_aircraft_image_url(manufacturer, model)
    if image_url:
        enhanced["image_url"] = image_url
    
    # Add additional useful fields
    enhanced["display_name"] = f"{meta.get('manufacturerName', '')} {meta.get('model', '')}".strip()
    enhanced["category"] = categorize_aircraft(manufacturer, model)
    
    return enhanced

def get_aircraft_destination(icao24, callsign=None):
    """Get the most likely destination for an aircraft based on recent flights"""
    try:
        import time
        now = int(time.time())
        # Look at flights from the last 7 days
        begin = now - (7 * 24 * 60 * 60)
        end = now
        
        flights_url = f"https://opensky-network.org/api/flights/aircraft"
        params = {
            "icao24": icao24,
            "begin": begin,
            "end": end
        }
        
        headers, _ = get_auth_headers()
        
        resp = requests.get(flights_url, params=params, headers=headers, timeout=5)
        if resp.status_code == 200:
            flights = resp.json()
            if flights:
                # Get the most recent flight
                latest_flight = max(flights, key=lambda x: x.get("lastSeen", 0))
                est_arrival = latest_flight.get("estArrivalAirport")
                if est_arrival:
                    return get_airport_name(est_arrival)
        
        # If no recent flights, try to infer from callsign patterns
        # But be more conservative - only use this for very specific patterns
        if callsign:
            # Only use callsign inference for very clear patterns
            if callsign.startswith("EZY") or callsign.startswith("U2"):
                return "London Gatwick"  # EasyJet typically goes to Gatwick
            elif callsign.startswith("BAW") or callsign.startswith("BA"):
                return "London Heathrow"  # British Airways typically goes to Heathrow
            elif callsign.startswith("AFR") or callsign.startswith("AF"):
                return "Paris Charles de Gaulle"  # Air France typically goes to CDG
            elif callsign.startswith("DLH") or callsign.startswith("LH"):
                return "Frankfurt"  # Lufthansa typically goes to Frankfurt
            elif callsign.startswith("KLM"):
                # For KLM flights, try to infer destination from flight number patterns
                # This is a simplified approach - in reality, you'd need a comprehensive database
                flight_num = callsign[3:].strip()
                logging.info(f"KLM callsign: '{callsign}', extracted flight_num: '{flight_num}'")
                if flight_num.isdigit():
                    num = int(flight_num)
                    logging.info(f"KLM flight number: {num}")
                    # KLM flight number patterns (simplified)
                    if 1000 <= num <= 1999:
                        logging.info(f"KLM {num} -> Billund")
                        return "Billund"  # KLM flights 1000-1999 often go to Billund
                    elif 2000 <= num <= 2999:
                        logging.info(f"KLM {num} -> Copenhagen")
                        return "Copenhagen"  # KLM flights 2000-2999 often go to Copenhagen
                    elif 3000 <= num <= 3999:
                        logging.info(f"KLM {num} -> Stockholm")
                        return "Stockholm"  # KLM flights 3000-3999 often go to Stockholm
                    elif 4000 <= num <= 4999:
                        logging.info(f"KLM {num} -> Oslo")
                        return "Oslo"  # KLM flights 4000-4999 often go to Oslo
                    elif 5000 <= num <= 5999:
                        logging.info(f"KLM {num} -> Helsinki")
                        return "Helsinki"  # KLM flights 5000-5999 often go to Helsinki
                    elif 6000 <= num <= 6999:
                        logging.info(f"KLM {num} -> Berlin")
                        return "Berlin"  # KLM flights 6000-6999 often go to Berlin
                    elif 7000 <= num <= 7999:
                        logging.info(f"KLM {num} -> Munich")
                        return "Munich"  # KLM flights 7000-7999 often go to Munich
                    elif 8000 <= num <= 8999:
                        logging.info(f"KLM {num} -> Frankfurt")
                        return "Frankfurt"  # KLM flights 8000-8999 often go to Frankfurt
                    elif 9000 <= num <= 9999:
                        logging.info(f"KLM {num} -> Vienna")
                        return "Vienna"  # KLM flights 9000-9999 often go to Vienna
                    else:
                        logging.info(f"KLM {num} -> no pattern match")
                else:
                    logging.info(f"KLM flight number '{flight_num}' is not a digit")
                # If no pattern matches, don't assume destination
        
        return None
        
    except Exception as e:
        logging.error(f"Failed to get destination for {icao24}: {e}")
        return None

def generate_aircraft_image_url(manufacturer, model):
    """Generate aircraft image URL based on manufacturer and model"""
    # For now, return None to indicate no image is available
    # In production, you could integrate with:
    # - JetPhotos API (if available)
    # - Planespotters API (if available) 
    # - Your own aircraft image database
    # - A reliable stock photo service
    
    # The frontend will handle the fallback display when image_url is None
    return None

def categorize_aircraft(manufacturer, model):
    """Categorize aircraft by type"""
    if "boeing" in manufacturer or "airbus" in manufacturer:
        if any(x in model for x in ["737", "a320", "a319", "a321"]):
            return "Narrow-body"
        elif any(x in model for x in ["747", "777", "787", "a330", "a350", "a380"]):
            return "Wide-body"
    elif "embraer" in manufacturer:
        return "Regional"
    elif "bombardier" in manufacturer or "cseries" in model:
        return "Regional"
    elif "atr" in manufacturer:
        return "Turboprop"
    
    return "Unknown"

def get_airport_name(icao_code):
    """Convert ICAO airport code to readable airport name"""
    if not icao_code:
        return None
    
    # Common airport codes and their names
    airport_names = {
        "EHAM": "Amsterdam Schiphol",
        "LHR": "London Heathrow", 
        "LGW": "London Gatwick",
        "CDG": "Paris Charles de Gaulle",
        "ORY": "Paris Orly",
        "FRA": "Frankfurt",
        "MUC": "Munich",
        "FCO": "Rome Fiumicino",
        "MAD": "Madrid Barajas",
        "BCN": "Barcelona",
        "ZUR": "Zurich",
        "VIE": "Vienna",
        "BRU": "Brussels",
        "DUB": "Dublin",
        "CPH": "Copenhagen",
        "ARN": "Stockholm Arlanda",
        "OSL": "Oslo",
        "HEL": "Helsinki",
        "WAW": "Warsaw",
        "PRG": "Prague",
        "BUD": "Budapest",
        "IST": "Istanbul",
        "ATH": "Athens",
        "LIS": "Lisbon",
        "OPO": "Porto",
        "JFK": "New York JFK",
        "LGA": "New York LaGuardia", 
        "EWR": "Newark",
        "LAX": "Los Angeles",
        "SFO": "San Francisco",
        "ORD": "Chicago O'Hare",
        "DFW": "Dallas Fort Worth",
        "ATL": "Atlanta",
        "MIA": "Miami",
        "BOS": "Boston",
        "SEA": "Seattle",
        "DEN": "Denver",
        "LAS": "Las Vegas",
        "PHX": "Phoenix",
        "DTW": "Detroit",
        "MSP": "Minneapolis",
        "CLT": "Charlotte",
        "PHL": "Philadelphia",
        "BWI": "Baltimore",
        "IAD": "Washington Dulles",
        "DCA": "Washington Reagan",
        "SLC": "Salt Lake City",
        "PDX": "Portland",
        "SAN": "San Diego",
        "AUS": "Austin",
        "HOU": "Houston",
        "MCO": "Orlando",
        "TPA": "Tampa",
        "FLL": "Fort Lauderdale",
        "MCI": "Kansas City",
        "STL": "St. Louis",
        "CLE": "Cleveland",
        "PIT": "Pittsburgh",
        "CVG": "Cincinnati",
        "IND": "Indianapolis",
        "CMH": "Columbus",
        "MEM": "Memphis",
        "BNA": "Nashville",
        "RDU": "Raleigh",
        "RIC": "Richmond",
        "ORF": "Norfolk",
        "CHS": "Charleston",
        "SAV": "Savannah",
        "JAX": "Jacksonville",
        "RSW": "Fort Myers",
        "PBI": "West Palm Beach",
        "SRQ": "Sarasota",
        "GNV": "Gainesville",
        "TLH": "Tallahassee",
        "PNS": "Pensacola",
        "MOB": "Mobile",
        "BHM": "Birmingham",
        "HSV": "Huntsville",
        "MKE": "Milwaukee",
        "GRR": "Grand Rapids",
        "LAN": "Lansing",
        "FNT": "Flint",
        "TOL": "Toledo",
        "DAY": "Dayton",
        "LEX": "Lexington",
        "SDF": "Louisville",
        "EVV": "Evansville",
        "TYS": "Knoxville",
        "CHA": "Chattanooga",
        "AUG": "Augusta",
        "AGS": "Augusta Regional",
        "CSG": "Columbus",
        "MCN": "Macon",
        "AHN": "Athens",
        "WRB": "Warner Robins",
        "VLD": "Valdosta",
        "ABY": "Albany",
        "VAD": "Moody AFB",
        "VPS": "Valparaiso",
        "ECP": "Panama City",
        "DAB": "Daytona Beach",
        "SFB": "Sanford",
        "PIE": "St. Petersburg",
        "SPG": "St. Petersburg",
        "LAL": "Lakeland",
        "BOW": "Bartow",
        "EGNV": "Newcastle International",
        "EGLL": "London Heathrow",
        "EGGW": "London Luton",
        "EGSS": "London Stansted",
        "EGKK": "London Gatwick",
        "EGPH": "Edinburgh",
        "EGGD": "Bristol",
        "EGCC": "Manchester",
        "EGNT": "Newcastle",
        "EGSH": "Norwich",
        "EGNX": "East Midlands",
        "EGBB": "Birmingham"
    }
    
    return airport_names.get(icao_code.upper(), icao_code)

def create_fallback_metadata(icao24):
    """Create fallback metadata when OpenSky doesn't have aircraft info"""
    # Try to extract some info from ICAO24 if possible
    # ICAO24 is 6 hex characters, first 3 are manufacturer code
    manufacturer_code = icao24[:3].upper() if len(icao24) >= 3 else ""
    
    # Common manufacturer codes
    manufacturer_codes = {
        "4CA": "Airbus",
        "4CC": "Airbus", 
        "4D0": "Boeing",
        "4D1": "Boeing",
        "4D2": "Boeing",
        "781": "Boeing",  # Chinese aircraft often use this
        "782": "Boeing",  # Chinese aircraft often use this
        "7C0": "Boeing",
        "7C1": "Boeing",
        "7C2": "Boeing",
        "7C3": "Boeing",
        "7C4": "Boeing",
        "7C5": "Boeing",
        "7C6": "Boeing",
        "7C7": "Boeing",
        "7C8": "Boeing",
        "7C9": "Boeing",
        "7CA": "Boeing",
        "7CB": "Boeing",
        "7CC": "Boeing",
        "7CD": "Boeing",
        "7CE": "Boeing",
        "7CF": "Boeing",
        "7D0": "Boeing",
        "7D1": "Boeing",
        "7D2": "Boeing",
        "7D3": "Boeing",
        "7D4": "Boeing",
        "7D5": "Boeing",
        "7D6": "Boeing",
        "7D7": "Boeing",
        "7D8": "Boeing",
        "7D9": "Boeing",
        "7DA": "Boeing",
        "7DB": "Boeing",
        "7DC": "Boeing",
        "7DD": "Boeing",
        "7DE": "Boeing",
        "7DF": "Boeing",
    }
    
    manufacturer = manufacturer_codes.get(manufacturer_code, "Unknown")
    
    # Try to guess aircraft type based on ICAO24 patterns
    if manufacturer == "Boeing":
        if icao24.startswith("781") or icao24.startswith("782"):
            model = "Boeing 737/777/787"  # Common Chinese Boeing types
        else:
            model = "Boeing Aircraft"
    elif manufacturer == "Airbus":
        model = "Airbus A320/A330/A350"
    else:
        model = "Commercial Aircraft"
    
    return {
        "icao24": icao24,
        "manufacturerName": manufacturer,
        "manufacturer": manufacturer,
        "model": model,
        "type": model,
        "registration": f"ICAO:{icao24}",
        "operator": "Unknown",
        "display_name": f"{manufacturer} {model}",
        "category": "Commercial",
        "image_url": None
    }

@app.get("/aircraft_meta")
@require_admin_auth
def aircraft_meta():
    """Enhanced aircraft metadata lookup by ICAO24 or URL passthrough"""
    icao24 = request.args.get("icao24", "").strip()
    url = request.args.get("url", "").strip()
    
    if icao24:
        meta = _get_aircraft_meta(icao24)
        if not meta:
            return jsonify({
                "icao24": icao24.strip().lower(),
                "manufacturerName": None,
                "model": None,
            })
        return jsonify(enhance_aircraft_metadata(meta))
    
    elif url:
        # URL passthrough (original functionality)
        u = urlparse(url)
        host = u.netloc.split(":")[0].lower()
        if host not in ALLOWED_NETLOCS and not host.endswith("opensky-network.org"):
            return jsonify({"error": f"host not allowed: {host}"}), 403
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent":"plane-noise-dashboard/1.0","Accept":"application/json"})
            if r.status_code != 200:
                return jsonify({"error":"upstream", "status": r.status_code, "body": r.text[:600]}), 502
            return Response(r.content, status=200, content_type=r.headers.get("Content-Type", "application/json"))
        except requests.RequestException as e:
            return jsonify({"error": "request_exception", "details": str(e)}), 502
    
    else:
        return jsonify({"error":"missing icao24 or url"}), 400

@app.get("/aircraft_flights")
@require_admin_auth
def aircraft_flights():
    """Get historical flight data for an aircraft including destination"""
    icao24 = request.args.get("icao24")
    begin = request.args.get("begin")  # Unix timestamp
    end = request.args.get("end")      # Unix timestamp
    
    if not icao24:
        return jsonify({"error": "icao24 required"}), 400
    
    # Default to last 7 days if no time range specified
    if not begin or not end:
        import time
        now = int(time.time())
        end = now
        begin = now - (7 * 24 * 60 * 60)  # 7 days ago
    
    try:
        # Use OpenSky flights API to get historical flight data
        flights_url = f"https://opensky-network.org/api/flights/aircraft"
        params = {
            "icao24": icao24,
            "begin": begin,
            "end": end
        }
        
        # Add authentication if available
        headers, _ = get_auth_headers()
        
        resp = requests.get(flights_url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        flights_data = resp.json()
        
        # Process flights to extract destination information
        processed_flights = []
        for flight in flights_data:
            processed_flight = {
                "icao24": flight.get("icao24"),
                "firstSeen": flight.get("firstSeen"),
                "lastSeen": flight.get("lastSeen"),
                "estDepartureAirport": flight.get("estDepartureAirport"),
                "estArrivalAirport": flight.get("estArrivalAirport"),
                "callsign": flight.get("callsign"),
                "estDepartureAirportHorizDistance": flight.get("estDepartureAirportHorizDistance"),
                "estArrivalAirportHorizDistance": flight.get("estArrivalAirportHorizDistance"),
                "departureAirportCandidatesCount": flight.get("departureAirportCandidatesCount"),
                "arrivalAirportCandidatesCount": flight.get("arrivalAirportCandidatesCount")
            }
            processed_flights.append(processed_flight)
        
        return jsonify({
            "icao24": icao24,
            "flights": processed_flights,
            "count": len(processed_flights)
        })
        
    except Exception as e:
        logging.error(f"Failed to fetch flights for {icao24}: {e}")
        return jsonify({"error": str(e), "icao24": icao24}), 500

@app.get("/test_destination")
@require_admin_auth
def test_destination():
    """Test destination lookup for an aircraft"""
    icao24 = request.args.get("icao24")
    callsign = request.args.get("callsign", "")
    
    if not icao24:
        return jsonify({"error": "icao24 required"}), 400
    
    destination = get_aircraft_destination(icao24, callsign)
    return jsonify({
        "icao24": icao24,
        "callsign": callsign,
        "destination": destination
    })

@app.get("/flight_track")
@require_admin_auth
def get_flight_track():
    """Get flight track waypoints for an aircraft"""
    icao24 = request.args.get("icao24")
    begin = request.args.get("begin")
    end = request.args.get("end")
    
    if not icao24:
        return jsonify({"error": "Missing icao24 parameter"}), 400
    
    try:
        import time
        if not begin:
            # Default to last 2 hours if no begin time specified
            begin = int(time.time()) - (2 * 60 * 60)
        if not end:
            end = int(time.time())
        
        begin = int(begin)
        end = int(end)
        
        # Use OpenSky tracks API to get detailed flight track
        tracks_url = f"https://opensky-network.org/api/tracks/all"
        params = {
            "icao24": icao24,
            "time": begin
        }
        
        headers, _ = get_auth_headers()
        # For tracks API, we need OAuth token
        if not headers.get('Authorization'):
            return jsonify({"error": "OAuth authentication required for tracks API"}), 401
        
        resp = requests.get(tracks_url, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": f"OpenSky tracks API error: {resp.status_code}"}), 502
        
        tracks_data = resp.json()
        if not tracks_data or "tracks" not in tracks_data:
            return jsonify({"waypoints": [], "message": "No track data available"})
        
        # Find the track for this aircraft
        aircraft_track = None
        for track in tracks_data["tracks"]:
            if track.get("icao24") == icao24:
                aircraft_track = track
                break
        
        if not aircraft_track:
            return jsonify({"waypoints": [], "message": "No track data available for this aircraft"})
        
        # Extract waypoints from the track data
        waypoints = []
        if "path" in aircraft_track and aircraft_track["path"]:
            for i, point in enumerate(aircraft_track["path"]):
                if len(point) >= 2:  # Ensure we have at least lat, lon
                    waypoints.append({
                        "lat": point[0],
                        "lon": point[1],
                        "altitude": point[2] if len(point) > 2 else None,
                        "timestamp": point[3] if len(point) > 3 else None,
                        "point_index": i
                    })
        
        return jsonify({
            "icao24": icao24,
            "callsign": aircraft_track.get("callsign", ""),
            "waypoints": waypoints,
            "flight_info": {
                "startTime": aircraft_track.get("startTime"),
                "endTime": aircraft_track.get("endTime")
            }
        })
        
    except Exception as e:
        logging.error(f"Failed to get flight track for {icao24}: {e}")
        return jsonify({"error": f"Failed to get flight track: {str(e)}"}), 500

# ---- Sensor.Community proxy --------------------------------------------------

@app.get("/sensor_api")
@require_admin_auth
def sensor_api():
    """Proxy noise API for a single sensor id.
    Example: /sensor_api?id=69616&limit=500
    """
    try:
        sid = request.args.get("id", "").strip()
        limit = request.args.get("limit", "200")
        if not sid.isdigit():
            return jsonify({"error":"bad id"}), 400
        
        # Get the current noise API configuration
        noise_config = get_api_config('noise')
        if not noise_config:
            return jsonify({"error": "Noise API not configured"}), 500
        
        # Use the configured API system
        response = make_api_request(
            api_name='noise',
            endpoint='sensor_data',
            params={'sensor_id': sid, 'limit': limit},
            cache_key=f"noise_sensor_{sid}_{limit}"
        )
        
        if response.status_code != 200:
            return jsonify({"error":"upstream", "status": response.status_code, "body": response.text[:800]}), 502
        
        # Transform the data to the format expected by the frontend
        raw_data = response.json()
        transformed_data = []
        
        for item in raw_data:
            # Find the noise_LAeq value
            noise_value = None
            for sensor_value in item.get('sensordatavalues', []):
                if sensor_value.get('value_type') == 'noise_LAeq':
                    noise_value = float(sensor_value.get('value', 0))
                    break
            
            if noise_value is not None:
                # Convert timestamp to Unix timestamp
                timestamp_str = item.get('timestamp', '')
                try:
                    from datetime import datetime
                    dt = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                    timestamp = dt.timestamp()
                except:
                    timestamp = time.time()
                
                transformed_data.append({
                    'timestamp': timestamp,
                    'value': noise_value
                })
        
        return jsonify(transformed_data)
    except requests.RequestException as e:
        return jsonify({"error": "request_exception", "details": str(e)}), 502

# ---- Append log (JSONL) and read recent -------------------------------------

def _rotate_log_if_needed():
    try:
        if os.path.exists(LOG_FILE):
            size_mb = os.path.getsize(LOG_FILE) / (1024*1024)
            if size_mb > LOG_MAX_MB:
                ts = time.strftime("%Y%m%d-%H%M%S")
                os.rename(LOG_FILE, LOG_FILE + "." + ts)
    except Exception as e:
        logging.warning("log rotate failed: %s", e)

@app.post("/append_log")
@require_admin_auth
def append_log():
    try:
        ev = request.get_json(silent=True) or {}
        ev["ts"] = ev.get("ts") or time.time()
        
        # Ensure required fields exist
        if "type" not in ev:
            ev["type"] = "unknown"
        if "icao24" not in ev:
            ev["icao24"] = "unknown"
        
        # Log to database instead of JSONL
        event_id = db.log_aircraft_event(ev)
        
        logging.info("append_log -> database (id: %s, type: %s, icao24: %s)", event_id, ev.get("type"), ev.get("icao24"))
        return jsonify({"ok": True, "logged": ev, "id": event_id})
    except Exception as e:
        logging.error("append_log failed: %s", str(e))
        return jsonify({"error":"log_write_failed","details":str(e)}), 500

@app.get("/recent_events")
def recent_events():
    try:
        limit = max(1, min(int(request.args.get("limit", "200")), 1000))  # the page asks for 1000 and pages them itself
        event_type = request.args.get("type")  # Optional filter by event type
        
        # Get events from database
        events = db.get_recent_events(limit, event_type)
        sensor = _weather_sensor_point()
        advice = _weather_advice_enabled()
        zone_test = _zone_membership_test()
        
        # Convert database rows to the expected format.
        # Sentence ids are classified here from the stored columns. They are not stored.
        items = []
        for event in events:
            sentences = classify_weather_sentences(event, sensor) if advice else []
            item = {
                "id": event.get("id"),
                "ts": event.get("timestamp"),
                "type": event.get("type"),
                "icao24": event.get("icao24"),
                "callsign": event.get("callsign"),
                "distance_km": event.get("distance_km"),
                "altitude_m": event.get("altitude_m"),
                "spl_db": event.get("spl_db"),
                "spl_peak_ts": event.get("spl_peak_ts"),
                "spl_max_db": event.get("spl_max_db"),
                "route_kind": event.get("route_kind"),
                "route_airport": event.get("route_airport"),
                "route_runway": event.get("route_runway"),
                "route_label": event.get("route_label"),
                "route_confidence": event.get("route_confidence"),
                "speed_kmh": event.get("speed_kmh"),
                "heading_deg": event.get("heading_deg"),
                "destination": event.get("destination"),
                "manufacturer": event.get("manufacturer"),
                "model": event.get("model"),
                "registration": event.get("registration"),
                "operator": event.get("operator"),
                "image_url": event.get("image_url"),
                "latitude": event.get("latitude"),
                "longitude": event.get("longitude"),
                "weather_mark": bool(sentences),
                "in_zone": bool(event.get("in_zone")) if event.get("in_zone") is not None
                           else zone_test(event.get("latitude"), event.get("longitude"), event.get("distance_km")),
                "weather_sentences": sentences,
            }
            items.append(item)
        
        return jsonify({"events": items})
    except Exception as e:
        return jsonify({"error":"log_read_failed","details":str(e)}), 500

def _zone_membership_test():
    """A function telling whether a stored position counts as inside the drawn zone.

    With a polygon: the point lies inside it. Without one: the closest ground
    distance is within the noise range. The public table defaults to this.
    """
    points = []
    try:
        if db.get_admin_setting('inner_zone_type', 'circle') == 'polygon':
            points = json.loads(db.get_admin_setting('detection_zone_points', '[]') or '[]')
    except (TypeError, ValueError):
        points = []
    try:
        inner_km = float(db.get_admin_setting('noise_assoc_inner_km', '2.5')) + 0.2
    except (TypeError, ValueError):
        inner_km = 2.7
    if len(points) >= 3:
        def test(lat, lon, distance_km):
            if lat is None or lon is None:
                return distance_km is not None and float(distance_km) <= inner_km
            try:
                return bool(point_in_polygon(float(lat), float(lon), points))
            except (TypeError, ValueError):
                return False
        return test
    return lambda lat, lon, distance_km: distance_km is not None and float(distance_km) <= inner_km


community.zone_test = _zone_membership_test  # the history send judges old passes with it


@app.get("/api/event/<int:event_id>")
def get_event_details(event_id: int):
    """Get detailed information about a specific event by ID"""
    try:
        event = db.get_event_by_id(event_id)
        if not event:
            return jsonify({"error": "event_not_found"}), 404
        
        # Convert database row to the expected format
        event_data = {
            "id": event.get("id"),
            "ts": event.get("timestamp"),
            "type": event.get("type"),
            "icao24": event.get("icao24"),
            "callsign": event.get("callsign"),
            "distance_km": event.get("distance_km"),
            "altitude_m": event.get("altitude_m"),
            "spl_db": event.get("spl_db"),
            "spl_peak_ts": event.get("spl_peak_ts"),
            "spl_max_db": event.get("spl_max_db"),
            "route_kind": event.get("route_kind"),
            "route_airport": event.get("route_airport"),
            "route_runway": event.get("route_runway"),
            "route_label": event.get("route_label"),
            "route_confidence": event.get("route_confidence"),
            "speed_kmh": event.get("speed_kmh"),
            "heading_deg": event.get("heading_deg"),
            "destination": event.get("destination"),
            "manufacturer": event.get("manufacturer"),
            "model": event.get("model"),
            "registration": event.get("registration"),
            "operator": event.get("operator"),
            "image_url": event.get("image_url"),
            "latitude": event.get("latitude"),
            "longitude": event.get("longitude"),
            "bucket_5m": event.get("bucket_5m")
        }
        
        return jsonify({"event": event_data})
    except Exception as e:
        return jsonify({"error": "event_details_failed", "details": str(e)}), 500

NOISE_BANDS = (50.0, 55.0, 60.0)       # the thresholds people complain and legislate in
NIGHT_HOURS = (23, 7)                  # local night: 23:00 up to 07:00


def _local_day_bounds(zone, days):
    """[start, end) epoch pairs for the last `days` local days, oldest first, each with its date."""
    from datetime import datetime as _dt, timedelta as _td
    now_local = _dt.now(zone)
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for back in range(days - 1, -1, -1):
        start = zone.normalize(today - _td(days=back)) if hasattr(zone, 'normalize') else today - _td(days=back)
        end = zone.normalize(start + _td(days=1)) if hasattr(zone, 'normalize') else start + _td(days=1)
        out.append((start.date().isoformat(), start.timestamp(), end.timestamp()))
    return out


def noise_daily_summary(days=14):
    """Per local day: passes, passes with a decibel, counts per band, night passes, the loudest."""
    days = max(1, min(int(days), 92))
    zone = pytz.timezone(_place_timezone_name())
    bounds = _local_day_bounds(zone, days)
    rows = db.get_passes_between(bounds[0][1], bounds[-1][2])
    from datetime import datetime as _dt
    per_day = {}
    for date_str, start, end in bounds:
        per_day[date_str] = {"date": date_str, "passes": 0, "with_db": 0, "band_50": 0, "band_55": 0, "band_60": 0,
                             "night_50": 0, "loudest": None}
    for row in rows:
        ts = float(row.get("timestamp") or 0)
        local = _dt.fromtimestamp(ts, zone)
        day = per_day.get(local.date().isoformat())
        if day is None:
            continue
        day["passes"] += 1
        spl = row.get("spl_db")
        if spl is None:
            continue
        spl = float(spl)
        day["with_db"] += 1
        if spl >= NOISE_BANDS[2]:
            day["band_60"] += 1
        elif spl >= NOISE_BANDS[1]:
            day["band_55"] += 1
        elif spl >= NOISE_BANDS[0]:
            day["band_50"] += 1
        night = local.hour >= NIGHT_HOURS[0] or local.hour < NIGHT_HOURS[1]
        if night and spl >= NOISE_BANDS[0]:
            day["night_50"] += 1
        if day["loudest"] is None or spl > day["loudest"]["spl_db"]:
            day["loudest"] = {"spl_db": spl, "spl_max_db": row.get("spl_max_db"), "callsign": row.get("callsign"),
                              "ts": ts, "model": row.get("model"), "registration": row.get("registration"),
                              "icao24": row.get("icao24"), "id": row.get("id")}
    series = [per_day[d] for d, _s, _e in bounds]
    loudest_period = max((d["loudest"] for d in series if d["loudest"]), key=lambda x: x["spl_db"], default=None)
    return {"days": days, "timezone": zone.zone, "bands": list(NOISE_BANDS), "night_hours": list(NIGHT_HOURS),
            "series": series, "today": series[-1], "loudest": loudest_period}


@app.get("/api/stats/noise/daily")
def api_noise_daily():
    try:
        days = request.args.get("days", 14, type=int) or 14
        payload = noise_daily_summary(days)
        resp = jsonify(payload)
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp
    except Exception as e:
        return jsonify({"error": "noise_daily_failed", "details": str(e)}), 500


@app.get("/api/callsigns/<callsign>/passes")
def callsign_passes(callsign: str):
    try:
        limit = max(1, min(request.args.get("limit", 50, type=int) or 50, 200))
        before = request.args.get("before", type=float)  # a timestamp: the page asks for passes older than its last row
        rows = db.get_passes_for_callsign(callsign, limit, before)
        return jsonify({"callsign": callsign.strip().upper(), "more": len(rows) >= limit, "passes": [{
            "id": r["id"], "ts": r["timestamp"], "icao24": r["icao24"], "registration": r["registration"],
            "model": r["model"], "operator": r["operator"], "distance_km": r["distance_km"], "altitude_m": r["altitude_m"],
            "speed_kmh": r["speed_kmh"], "heading_deg": r["heading_deg"], "spl_db": r["spl_db"],
            "spl_max_db": r["spl_max_db"], "spl_peak_ts": r["spl_peak_ts"],
        } for r in rows]})
    except Exception as e:
        return jsonify({"error": "passes_failed", "details": str(e)}), 500


@app.get("/api/callsigns/<callsign>/stats")
def callsign_stats(callsign: str):
    try:
        stats = db.get_callsign_stats(callsign)
        if not stats:
            return jsonify({"error": "not_found"}), 404
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": "stats_failed", "details": str(e)}), 500

@app.get("/api/stats/planes/heatmap")
def planes_heatmap():
    try:
        from config import Config
        from datetime import datetime, timezone, timedelta
        date_str = request.args.get('date')  # optional YYYY-MM-DD
        start_date = request.args.get('start_date')  # optional start date for week range
        end_date = request.args.get('end_date')  # optional end date for week range
        sensor_timezone_offset = get_sensor_timezone_offset()  # Use sensor timezone from config
        
        def _parse_local_date(s: str) -> datetime:
            # Accept both YYYY-MM-DD and DD/MM/YYYY
            fmts = ["%Y-%m-%d", "%d/%m/%Y"]
            last_err = None
            for f in fmts:
                try:
                    return datetime.strptime(s, f)
                except Exception as e:
                    last_err = e
            raise last_err or ValueError("Invalid date")

        # Use date_str if provided, otherwise use start_date/end_date for week ranges
        if date_str:
            data = db.get_planes_heatmap(date_str=date_str, sensor_timezone_offset=sensor_timezone_offset)
        elif start_date and end_date:
            try:
                # Interpret input dates as sensor-local midnight
                sensor_offset = int(sensor_timezone_offset)
                tz_delta = timedelta(hours=sensor_offset)
                # start at 00:00 local (support two formats)
                s_local = _parse_local_date(start_date)
                e_local = _parse_local_date(end_date)
                e_local = e_local + timedelta(days=1)  # exclusive end
                # Convert to UTC
                s_utc = (s_local - tz_delta).replace(tzinfo=timezone.utc)
                e_utc = (e_local - tz_delta).replace(tzinfo=timezone.utc)
                data = db.get_planes_heatmap_range(int(s_utc.timestamp()), int(e_utc.timestamp()), sensor_timezone_offset=sensor_timezone_offset)
            except Exception:
                # As a safe fallback, still try inclusive date filter in SQL
                data = db.get_planes_heatmap(start_date=start_date, end_date=end_date, sensor_timezone_offset=sensor_timezone_offset)
        else:
            data = db.get_planes_heatmap(sensor_timezone_offset=sensor_timezone_offset)
        
        return jsonify({"heatmap": data, "date": date_str, "sensor_timezone_offset": sensor_timezone_offset})
    except Exception as e:
        return jsonify({"error": "heatmap_failed", "details": str(e)}), 500

@app.get("/api/stats/planes/details")
def planes_details():
    try:
        from config import Config
        date_str = request.args.get('date')  # YYYY-MM-DD
        hour_str = request.args.get('hour')  # 00-23
        sensor_timezone_offset = get_sensor_timezone_offset()  # Use sensor timezone from config
        if not date_str or not hour_str:
            return jsonify({"error": "date and hour parameters required"}), 400
        
        try:
            hour = int(hour_str)
            if hour < 0 or hour > 23:
                return jsonify({"error": "hour must be between 0 and 23"}), 400
        except ValueError:
            return jsonify({"error": "invalid hour format"}), 400
        
        events = db.get_aircraft_events_by_date_hour(date_str, hour, sensor_timezone_offset)
        
        # Convert to the format expected by the frontend
        aircraft = []
        for event in events:
            aircraft.append({
                "timestamp": event["timestamp"],
                "callsign": event["callsign"],
                "distance_km": event["distance_km"],
                "altitude_m": event["altitude_m"],
                "manufacturer": event["manufacturer"],
                "model": event["model"],
                "operator": event["operator"],
                "icao24": event["icao24"]
            })
        
        return jsonify({"aircraft": aircraft})
    except Exception as e:
        return jsonify({"error": "details_failed", "details": str(e)}), 500

@app.get("/callsign.html")
def callsign_page():
    try:
        return send_file('callsign.html')
    except Exception as e:
        logging.error(f"Failed to serve callsign.html: {e}")
        return "Not Found", 404

@app.get("/admin/analytics")
@require_admin_auth
def admin_analytics():
    """Serve the analytics dashboard"""
    try:
        return send_admin_page('admin_analytics.html')
    except Exception as e:
        logging.error(f"Failed to serve admin_analytics.html: {e}")
        return "Not Found", 404



# ---- Server-side correlation and logging ------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    to_rad = math.radians
    dphi = to_rad(lat2 - lat1)
    dlam = to_rad(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(to_rad(lat1))*math.cos(to_rad(lat2))*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

_air_meta_cache = {}
_opensky_meta_gone = False

def _get_aircraft_meta(icao24: str):
    """Make and model for one ICAO address: the local registry first, then OpenSky's
    catalog while it still answers (a 410 means it was removed)."""
    global _opensky_meta_gone
    key = (icao24 or "").strip().lower()
    if not key:
        return None
    if key in _air_meta_cache:
        return _air_meta_cache[key]
    try:
        local = db.lookup_aircraft(key)
    except Exception:
        local = None
    if local:
        meta = {
            "manufacturerName": local.get("manufacturer"),
            "model": local.get("model") or local.get("typecode"),
            "typecode": local.get("typecode"),
            "registration": local.get("registration"),
            "operator": local.get("operator"),
            "source": "registry",
        }
        _air_meta_cache[key] = meta
        return meta
    if _opensky_meta_gone:
        return None
    url = f"https://opensky-network.org/api/metadata/aircraft/icao/{key}"
    try:
        headers, _mode = get_auth_headers()
        headers = dict(headers)
        headers["User-Agent"] = "plane-noise-dashboard/1.0"
        headers["Accept"] = "application/json"
        response = requests.get(url, timeout=10, headers=headers)
    except Exception:
        return None
    if response.status_code == 410:
        _opensky_meta_gone = True
        logging.warning("OpenSky aircraft metadata returned 410. Make and model stay blank.")
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    _air_meta_cache[key] = body
    return body

@app.post("/log_match")
@require_admin_auth
def log_match():
    """Server-side: given SPL and optional lat/lon, fetch planes, filter, and log events.
    Body JSON: { spl_db: number, ts?: seconds, lat?: number, lon?: number, radius_km?: number, alt_max_m?: number }
    """
    try:
        body = request.get_json(silent=True) or {}
        spl_db = body.get("spl_db")
        if spl_db is None:
            return jsonify({"error":"missing spl_db"}), 400
        ts = float(body.get("ts") or time.time())
        lat = float(body.get("lat") or SENSOR_LAT)
        lon = float(body.get("lon") or SENSOR_LON)
        
        # Get settings from database or use defaults
        radius_km = float(body.get("radius_km") or db.get_admin_setting("detection_radius_km", "8.0"))
        alt_max_m = float(body.get("alt_max_m") or db.get_admin_setting("max_altitude_m", "2000"))

        # Build bbox similar to frontend
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * max(math.cos(math.radians(lat)), 1e-6))
        
        # Use direct API call to bypass rate limiting
        try:
            import requests
            url = f"https://opensky-network.org/api/states/all?lamin={lat-lat_delta}&lomin={lon-lon_delta}&lamax={lat+lat_delta}&lomax={lon+lon_delta}"
            headers = {'User-Agent': 'plane-noise-dashboard/1.0'}
            
            response = requests.get(url, headers=headers, timeout=20)
            
            if response.status_code != 200:
                return jsonify({"error":"opensky", "status": response.status_code}), 502
            j = response.json()
            states = j.get("states") or []
        except Exception as e:
            return jsonify({"error":"opensky", "details": str(e)}), 502
        logged = 0
        matches = []
        for s in states:
            try:
                icao24 = s[0]
                callsign = (s[1] or "").strip()
                plon = float(s[5]); plat = float(s[6])
                baro = s[7]; geo = s[13]
                alt = geo if geo is not None else baro
                # Try to get destination from historical flights or callsign
                destination = get_aircraft_destination(icao24, callsign)
                logging.info(f"Destination for {icao24} ({callsign}): {destination}")
                if plon is None or plat is None:
                    continue
                dist = _haversine_km(lat, lon, plat, plon)
                if dist <= radius_km and (alt is None or float(alt) <= alt_max_m):
                    meta = _get_aircraft_meta(icao24)
                    ev = {
                        "ts": ts,
                        "type": body.get("type", "event"),  # Use the type from the request body
                        "callsign": callsign,
                        "icao24": icao24,
                        "distance_km": round(dist, 3),
                        "altitude_m": None if alt is None else int(round(float(alt))),
                        "spl_db": float(spl_db),
                        "manufacturer": (meta or {}).get("manufacturerName") or (meta or {}).get("manufacturer"),
                        "model": (meta or {}).get("model") or (meta or {}).get("type"),
                        "registration": (meta or {}).get("registration"),
                        "operator": (meta or {}).get("operator"),
                        "destination": get_airport_name(destination),
                        "image_url": (meta or {}).get("image_url")
                    }
                    # Log to database instead of file
                    event_id = db.log_aircraft_event(ev)
                    logged += 1
                    matches.append({"icao24": icao24, "callsign": callsign, "dist_km": dist, "id": event_id})
            except Exception:
                continue
        return jsonify({"ok": True, "logged": logged, "matches": matches})
    except Exception as e:
        logging.exception("log_match error")
        return jsonify({"error":"log_match_failed","details":str(e)}), 500

# Admin endpoints for managing settings
@app.get("/api/admin/settings")
@require_admin_auth
def get_admin_settings():
    """Get all admin settings"""
    try:
        settings = db.get_all_admin_settings()
        public = {
            key: value
            for key, value in settings.items()
            if not key.startswith(_SECRET_PREFIX) and key != "api_secrets_imported"
        }
        return jsonify(public)
    except Exception as e:
        logging.error(f"Failed to get admin settings: {e}")
        return jsonify({"error": "Failed to get settings"}), 500

def _confirmed(token: str):
    body = request.get_json(silent=True) or {}
    return body.get("confirm") == token


@app.post("/api/admin/clear-data")
@require_admin_auth
def api_admin_clear_data():
    """Delete recorded passes, visitor stats, and API usage counts."""
    if not _confirmed("clear-events"):
        return jsonify({"error": "confirmation_required"}), 400
    try:
        removed = db.clear_recorded_data()
        global latest_noise_data, noise_data_history
        latest_noise_data = None
        noise_data_history = []
        logging.warning("Admin cleared recorded data: %s", removed)
        return jsonify({"ok": True, "removed": removed})
    except Exception:
        logging.exception("clear recorded data failed")
        return jsonify({"error": "clear_failed"}), 500


@app.post("/api/admin/reset-system")
@require_admin_auth
def api_admin_reset_system():
    """Clear recorded data and restore detection, visitor, and zone defaults."""
    if not _confirmed("reset-settings"):
        return jsonify({"error": "confirmation_required"}), 400
    try:
        removed = db.clear_recorded_data()
        restored = db.reset_operational_settings()
        global latest_noise_data, noise_data_history
        latest_noise_data = None
        noise_data_history = []
        logging.warning("Admin reset operational settings; cleared %s", removed)
        return jsonify({"ok": True, "removed": removed, "restored": restored})
    except Exception:
        logging.exception("reset system failed")
        return jsonify({"error": "reset_failed"}), 500


@app.post("/api/admin/settings")
@require_admin_auth
def update_admin_settings():
    """Update admin settings"""
    try:
        settings = request.get_json(silent=True) or {}
        if "site_timezone" in settings:
            zone = str(settings["site_timezone"]).strip()
            if zone not in pytz.all_timezones_set:
                return jsonify({"error": "invalid_timezone"}), 400
            settings["site_timezone"] = zone
        if "site_country" in settings:
            country = str(settings["site_country"]).strip().lower()
            flag = os.path.join(os.path.dirname(__file__), "static", "flags", "4x3", f"{country}.svg")
            if not re.fullmatch(r"[a-z]{2}", country) or not os.path.isfile(flag):
                return jsonify({"error": "invalid_country"}), 400
            settings["site_country"] = country
        if "site_place_name" in settings:
            place = str(settings["site_place_name"]).strip()[:80]
            if not place or "<" in place or ">" in place:
                return jsonify({"error": "invalid_place"}), 400
            settings["site_place_name"] = place
        if "station_notes" in settings:
            notes = _clean_station_notes(settings["station_notes"])
            if notes is None:
                return jsonify({"error": "invalid_station_notes"}), 400
            settings["station_notes"] = json.dumps(notes, ensure_ascii=False)
        updated = []
        for key, value in settings.items():
            if key.startswith(_SECRET_PREFIX) or key == "api_secrets_imported":
                continue
            db.set_admin_setting(key, str(value))
            updated.append(key)
        
        # Reload API configurations if aircraft settings were updated
        if 'noise_assoc_inner_km' in updated or 'inner_zone_type' in updated:
            db.recompute_zone_flags(_zone_membership_test())
        if any(key.startswith('aircraft_') for key in updated):
            reload_api_configs()
        
        return jsonify({"ok": True, "updated": updated})
    except Exception:
        logging.exception("update admin settings failed")
        return jsonify({"error": "settings_update_failed"}), 500

@app.get("/admin/plane_settings")
@require_admin_auth
def get_plane_settings():
    """Get plane detection settings"""
    try:
        settings = db.get_plane_settings()
        return jsonify({"plane_settings": settings})
    except Exception:
        logging.exception("get plane settings failed")
        return jsonify({"error": "settings_failed"}), 500

@app.post("/admin/plane_settings")
@require_admin_auth
def update_plane_settings():
    """Update plane detection settings"""
    try:
        settings = request.get_json(silent=True) or {}
        db.update_plane_settings(settings)
        return jsonify({"ok": True, "updated": settings.get("id")})
    except Exception:
        logging.exception("update plane settings failed")
        return jsonify({"error": "settings_update_failed"}), 500

@app.get("/admin/stats")
def get_admin_stats():
    """Public summary used by the monitor, plus a few operational counters."""
    try:
        recent_events = db.get_recent_events(100)
        closest_approaches = db.get_closest_approaches(100)
        unique_aircraft = len({event.get('icao24') for event in recent_events if event.get('icao24')})
        aircraft_events = [event for event in recent_events if event.get('icao24')]
        noise_events = [event for event in recent_events if event.get('spl_db') not in (None, 0)]

        def event_ts(event):
            return event.get('timestamp') or event.get('ts') or 0

        last_aircraft_update = max(event_ts(event) for event in aircraft_events) if aircraft_events else None
        last_noise_update = max(event_ts(event) for event in noise_events) if noise_events else None
        noise_values = [event.get('spl_db') for event in recent_events if event.get('spl_db') is not None]
        avg_noise_db = round(sum(noise_values) / len(noise_values), 1) if noise_values else 0
        distances = [event.get('distance_km') for event in closest_approaches if event.get('distance_km') is not None]
        closest_approach_km = round(min(distances), 3) if distances else 0
        db_path = "plane_noise.db"
        stats = {
            "total_events": db.get_events_count(),
            "closest_approaches": len(closest_approaches),
            "unique_aircraft": unique_aircraft,
            "database_size_mb": os.path.getsize(db_path) / (1024 * 1024) if os.path.exists(db_path) else 0,
            "last_aircraft_update": last_aircraft_update,
            "last_noise_update": last_noise_update,
            "avg_noise_db": avg_noise_db,
            "closest_approach_km": closest_approach_km,
            "today": db.get_stats_since(time.time() - 86400),
        }
        return jsonify({"stats": stats})
    except Exception:
        logging.exception("admin stats failed")
        return jsonify({"error": "stats_failed"}), 500


THEME_FAMILIES = ("instrument", "early")
THEME_APPEARANCES = ("dark", "light", "contrast", "system")


def _theme_policy():
    """Stored appearance policy. Visitors read this without signing in."""
    theme = db.get_admin_setting("site_theme", "instrument") or "instrument"
    if theme not in THEME_FAMILIES:
        theme = "instrument"
    appearance = db.get_admin_setting("site_appearance", "dark") or "dark"
    if appearance not in THEME_APPEARANCES:
        appearance = "dark"
    choice = str(db.get_admin_setting("visitor_theme_choice", "true")).lower() == "true"
    appearance_choice = str(db.get_admin_setting("visitor_appearance_choice", "true")).lower() == "true"
    raw = db.get_admin_setting("visitor_themes", "instrument,early") or "instrument,early"
    offered = [part.strip() for part in raw.split(",") if part.strip() in THEME_FAMILIES]
    if theme not in offered:
        offered.insert(0, theme)
    return {
        "theme": theme,
        "appearance": appearance,
        "choice": choice,
        "appearance_choice": appearance_choice,
        "offered": offered,
        "themes": offered if choice else [theme],
    }


@app.get("/visitor/theme")
def visitor_theme():
    """Public theme policy: the site default, and which named themes visitors may choose."""
    return jsonify(_theme_policy())


@app.post("/api/admin/theme")
@require_admin_auth
def update_visitor_theme():
    data = request.get_json(silent=True) or {}
    if data.get("theme") in THEME_FAMILIES:
        db.set_admin_setting("site_theme", data["theme"])
    if data.get("appearance") in THEME_APPEARANCES:
        db.set_admin_setting("site_appearance", data["appearance"])
    if "choice" in data:
        db.set_admin_setting("visitor_theme_choice", "true" if data["choice"] else "false")
    if "appearance_choice" in data:
        db.set_admin_setting("visitor_appearance_choice", "true" if data["appearance_choice"] else "false")
    if isinstance(data.get("themes"), list):
        picked = [item for item in data["themes"] if item in THEME_FAMILIES]
        db.set_admin_setting("visitor_themes", ",".join(picked))
    return jsonify(_theme_policy())


@app.get("/visitor/settings")
def get_visitor_settings():
    """Get visitor page settings (public endpoint)"""
    try:
        settings = {
            "radius_km": float(db.get_admin_setting("visitor_radius_km", "8")),
            "detection_radius_km": float(db.get_admin_setting("radius_km") or db.get_admin_setting("detection_radius_km") or 8),
            "altitude_max_m": int(db.get_admin_setting("visitor_altitude_max_m", "2000")),
            "update_interval_seconds": int(db.get_admin_setting("visitor_update_interval_seconds", "10")),
            "weather_advice": _weather_advice_enabled(),
            "map_zoom": _public_map_zoom(),
            # What the public events table lists: zone (crossed the drawn zone or measured), measured, or all.
            "events_view": (db.get_admin_setting("public_events_view", "zone") or "zone") if db.get_admin_setting("public_events_view", "zone") in ("zone", "measured", "all") else "zone",
        }
        settings.update(public_place())
        settings["station_notes"] = public_station_notes()
        return jsonify(settings)
    except Exception as e:
        logging.error(f"Failed to get visitor settings: {e}")
        return jsonify({"error": "Failed to get visitor settings"}), 500


@app.post("/admin/update_aircraft_data")
@require_admin_auth
def update_aircraft_data():
    """Update existing aircraft records with current speed/heading data"""
    try:
        data = request.get_json()
        if not data or 'icao24' not in data:
            return jsonify({"error": "ICAO24 required"}), 400
        
        icao24 = data['icao24']
        speed_kmh = data.get('speed_kmh')
        heading_deg = data.get('heading_deg')
        
        # Update the database record
        success = db.update_aircraft_speed_heading(icao24, speed_kmh, heading_deg)
        
        if success:
            return jsonify({"message": f"Updated aircraft data for {icao24}"})
        else:
            return jsonify({"error": "Failed to update aircraft data"}), 500
            
    except Exception as e:
        logging.error(f"Failed to update aircraft data: {e}")
        return jsonify({"error": "Failed to update aircraft data"}), 500

# WWW to non-WWW redirect (SEO fix)
# The scraper guard: budgets per address, a trap link, escalating bans. Admin
# pages, logins, the setup and the local machine are exempt (see guard.py).
scraper_guard = guard.Guard(store=db)


@app.before_request
def guard_request():
    try:
        scraper_guard.enabled = (db.get_admin_setting("guard_enabled", "true") or "true").lower() != "false"
    except Exception:
        pass
    kind = guard.classify(request.path, request.method, bool(request.headers.get("X-API-Key")))
    if kind == "exempt":
        return None
    try:
        scraper_guard.set_allow(db.get_admin_setting("guard_allow", ""))
    except Exception:
        pass
    # The signed-in administrator is never judged: the owner looking at the
    # public page from the kitchen must not be mistaken for a scraper.
    if _admin_session_ok():
        return None
    verdict = scraper_guard.check(guard.client_ip(request.headers, request.remote_addr), kind, request.user_agent.string)
    g.guard_crawler = verdict.crawler
    if verdict.ok:
        return None
    headers = {"Retry-After": str(max(1, verdict.retry_after)), "Cache-Control": "no-store"}
    if kind == "api":
        return jsonify({"ok": False, "error": guard.MESSAGES.get(verdict.reason, "blocked"), "retry_after_seconds": verdict.retry_after}), verdict.status, headers
    return guard.html_answer(verdict), verdict.status, dict(headers, **{"Content-Type": "text/html; charset=utf-8"})


def _stats_timezone():
    try:
        return pytz.timezone(db.get_admin_setting("site_timezone", "UTC") or "UTC")
    except Exception:
        return None


visitor_stats = analytics.Stats(store=db, tz=_stats_timezone())
geo_lookup = geo.Geo()


@app.after_request
def guard_misses(response):
    """Refused answers feed the guard; answered pages and JSON feed the statistics."""
    path = request.path
    if response.status_code in (401, 403, 429) and not path.startswith(("/admin", "/auth/", "/api/admin/")):
        try:
            scraper_guard.miss(guard.client_ip(request.headers, request.remote_addr))
        except Exception:
            pass
    try:
        kind = guard.classify(path, request.method, bool(request.headers.get("X-API-Key")))
        if kind in ("page", "api", "trap") and request.method == "GET":
            ip = guard.client_ip(request.headers, request.remote_addr)
            agent = request.user_agent.string
            bot = None
            if response.status_code in (403, 429):
                bot = "blocked"
            elif getattr(g, "guard_crawler", False):
                bot = "crawler"
            elif kind == "page" and guard.is_script(agent):
                bot = "script"
            elif response.status_code != 200:
                kind = None
            if kind:
                country = city = None
                if kind == "page" and not bot:
                    country = geo_lookup.country(ip)
                    city = geo_lookup.city(ip)
                visitor_stats.count(kind, path, ip, agent, request.headers, own_host=request.host.split(":")[0], bot=bot,
                                    query=request.query_string.decode("utf-8", "replace"), country=country, city=city)
        visitor_stats.flush_if_due()
    except Exception:
        logging.exception("Visitor statistics could not count a request")
    return response


@app.before_request
def redirect_www():
    """Redirect www subdomain to non-www for SEO"""
    if request.host and request.host.startswith('www.'):
        # Get the domain without www
        domain = request.host[4:]  # Remove 'www.'
        # Preserve the protocol (http/https)
        protocol = 'https' if request.is_secure else 'http'
        # Build the redirect URL
        redirect_url = f"{protocol}://{domain}{request.full_path}"
        return redirect(redirect_url, code=301)  # 301 = permanent redirect

# HTML serving routes
def _public_header(active):
    """The one public masthead. Pages mark where it goes with <!--PUBLIC_HEADER-->."""
    path = os.path.join(os.path.dirname(__file__), "partials", "public_header.html")
    with open(path, encoding="utf-8") as handle:
        header = handle.read()
    if active == "community":
        header = header.replace(
            'href="/community" class="header-nav-link"',
            'href="/community" class="header-nav-link" aria-current="page"',
            1,
        )
    return header


def _apply_station_mark(page):
    notes = public_station_notes()
    if not notes:
        return page
    safe_title = escape(notes[0]["title"])
    return page.replace(
        '<span id="stationMark" class="station-mark" hidden></span>',
        f'<span id="stationMark" class="station-mark">{safe_title}</span>',
        1,
    )


STATION_URL_PLACEHOLDER = "https://station.example"


def public_base_url():
    """This station's public address: the community card's public URL, else the
    address the request came in on. Canonical links, social tags, the sitemap and
    robots.txt are written with it, so no install advertises another station."""
    configured = community.canonical_url(db.get_admin_setting("community_public_url", "") or "")
    if configured:
        return configured
    try:
        return request.url_root.rstrip("/")
    except RuntimeError:
        return ""


def _fill_station_url(text):
    return text.replace(STATION_URL_PLACEHOLDER, public_base_url())


def _station_file(name, mimetype):
    """A static file with the station's own address written in."""
    path = os.path.join(os.path.dirname(__file__), name)
    with open(path, encoding="utf-8") as handle:
        body = _fill_station_url(handle.read())
    response = Response(body, mimetype=mimetype)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


SETUP_BANNER = (
    '<div class="setup-banner" style="background:#e2a122;color:#1c2b22;padding:0.8rem 1.2rem;text-align:center;'
    'font-family:system-ui,sans-serif;font-size:1rem">This station is not set up yet. '
    '<a href="/setup" style="color:#1c2b22;font-weight:700">Open the setup</a> to create the administrator account, '
    'place the microphone and pick the feeds. It takes a few minutes.</div>'
)


def _with_setup_banner(page):
    """A fresh install tells the first visitor where to go, instead of an empty page."""
    try:
        pending = setup_state() == "account"
    except Exception:
        pending = False
    if not pending:
        return page
    i = page.find("<body")
    if i < 0:
        return page
    j = page.find(">", i)
    return page[:j + 1] + SETUP_BANNER + page[j + 1:]


def _render_public_index():
    """Public page, with the first station note written into the title, masthead, and map card."""
    path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(path, encoding="utf-8") as handle:
        page = _with_setup_banner(_apply_station_mark(_fill_station_url(handle.read()).replace("<!--PUBLIC_HEADER-->", _public_header("home"), 1)))
    notes = public_station_notes()
    if notes:
        title = notes[0]["title"]
        body = notes[0]["body"]
        safe_title = escape(title)
        safe_body = escape(body)
        lead = f"{title}. {body}".strip(" .")
        if len(lead) > 180:
            lead = lead[:177].rstrip() + "..."
        page = page.replace(
            "<title>Aircraft Noise — real noise, measured on local sensors</title>",
            f"<title>{safe_title} — Aircraft Noise</title>",
            1,
        )
        page = page.replace(
            'content="Aircraft Noise measures real aircraft noise on local sensors.',
            f'content="{escape(lead)} ',
            1,
        )
        page = page.replace(
            'content="Real aircraft noise, measured on local sensors and matched to the flights overhead."',
            f'content="{escape(lead)}"',
            1,
        )
        page = page.replace('content="Netherlands"', f'content="{safe_title}"', 1)
        page = page.replace(
            '"name": "Aircraft Noise",\n    "description": "Real aircraft noise, measured on local sensors and matched to the flights overhead."',
            '"name": ' + json.dumps(f"{title} — Aircraft Noise") + ",\n    " + '"description": ' + json.dumps(lead),
            1,
        )
        page = page.replace(
            'property="og:title" content="Aircraft Noise — real noise, measured on local sensors"',
            f'property="og:title" content="{safe_title} — Aircraft Noise"',
            1,
        )
        page = re.sub(
            r'<div id="mapStation" class="map-station" hidden>\s*<h2 id="mapStationTitle"></h2>\s*<p id="mapStationBody"></p>\s*</div>',
            '<div id="mapStation" class="map-station"><h2 id="mapStationTitle">' + safe_title + '</h2><p id="mapStationBody">' + safe_body + '</p></div>',
            page,
            count=1,
        )
    response = Response(page, mimetype="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route('/')
def index():
    """Serve the main landing page"""
    return _render_public_index()

@app.route('/public')
def public_index():
    """Serve the public landing page with privacy features"""
    return _render_public_index()

@app.route('/nl/')
def nl_index():
    """
    Redirect to main page with Dutch language preference.
    
    This route previously served nl.html (hardcoded Dutch version).
    Now redirects to the main page with translation system and sets Dutch language preference.
    """
    response = redirect('/')
    response.set_cookie('language', 'nl', max_age=31536000)  # 1 year
    return response

@app.route('/nl/public')
def nl_public_index():
    """
    Redirect to public page with Dutch language preference.
    
    This route previously served nl.html (hardcoded Dutch version).
    Now redirects to the public page with translation system and sets Dutch language preference.
    """
    response = redirect('/public')
    response.set_cookie('language', 'nl', max_age=31536000)  # 1 year
    return response

@app.route('/community')
def community_page():
    """Public map of monitors that opted in. Same masthead as the monitor."""
    path = os.path.join(os.path.dirname(__file__), "community.html")
    with open(path, encoding="utf-8") as handle:
        page = handle.read()
    page = _apply_station_mark(page.replace("<!--PUBLIC_HEADER-->", _public_header("community"), 1))
    response = Response(page, mimetype="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-cache"
    return response

@app.route('/nl/community')
def nl_community_page():
    response = redirect('/community?lang=nl')
    response.set_cookie('language', 'nl', max_age=31536000)
    return response

@app.get('/api/community/monitors')
def api_community_monitors():
    document = community.load_monitors()
    response = jsonify(document)
    response.headers['Cache-Control'] = 'public, max-age=300'
    return response

@app.get('/api/admin/community')
@require_admin_auth
def api_admin_community_status():
    return jsonify(community.admin_status())

@app.post('/api/admin/community')
@require_admin_auth
def api_admin_community_save():
    body, status = community.save_settings(request.get_json(silent=True) or {})
    return jsonify(body), status

@app.get('/api/admin/analytics/summary')
@require_admin_auth
def api_admin_analytics_summary():
    """Everything the analytics page shows, in one answer."""
    try:
        days = max(1, min(int(request.args.get("days", 30)), 400))
    except ValueError:
        days = 30
    visitor_stats.flush()
    day_rows = db.get_stats_days(days)
    hours = db.get_stats_hours(days)
    passes = db.get_passes_per_hour(days)
    today = datetime.now(_stats_timezone() or pytz.utc).strftime("%Y-%m-%d")
    today_row = next((r for r in day_rows if r["day"] == today), {"views": 0, "api_views": 0, "visitors": 0})
    return jsonify({
        "days": days,
        "online": visitor_stats.online(),
        "today": today_row,
        "totals": {"views": sum(r["views"] for r in day_rows), "api_views": sum(r["api_views"] for r in day_rows),
                   "visitors": sum(r["visitors"] for r in day_rows), "days_with_data": len(day_rows)},
        "timeline": day_rows,
        "hours": [{"hour_ts": h["hour_ts"], "views": h["views"], "visitors": h["visitors"],
                   "passes": passes.get(h["hour_ts"], (0, 0))[0], "loud": passes.get(h["hour_ts"], (0, 0))[1]} for h in hours],
        "noise": analytics.noise_against_visits([(h["hour_ts"], h["views"], h["visitors"]) for h in hours], passes),
        "dims": {kind: db.get_stats_dims(days, kind) for kind in analytics.KINDS},
        "bans": scraper_guard.bans(),
        "tables": db.stats_table_sizes(),
        "retain_days": analytics.RETAIN_DAYS,
        "geo": geo_lookup.status(),
    })

@app.post('/api/admin/analytics/geo-refresh')
@require_admin_auth
def api_admin_analytics_geo_refresh():
    try:
        result = geo_lookup.refresh()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:200]}), 502
    return jsonify({"ok": True, "result": result, "geo": geo_lookup.status()})

@app.post('/api/admin/analytics/drop-legacy')
@require_admin_auth
def api_admin_analytics_drop_legacy():
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "delete":
        return jsonify({"ok": False, "error": "confirm"}), 400
    db.drop_legacy_visitor_tables()
    return jsonify({"ok": True, "tables": db.stats_table_sizes()})

@app.get('/api/admin/guard')
@require_admin_auth
def api_admin_guard():
    return jsonify({
        "enabled": (db.get_admin_setting("guard_enabled", "true") or "true").lower() != "false",
        "allow": db.get_admin_setting("guard_allow", "") or "",
        "bans": scraper_guard.bans(),
        "limits": {"pages_per_minute": guard.PAGES_PER_MINUTE, "pages_per_hour": guard.PAGES_PER_HOUR,
                   "api_per_minute": guard.API_PER_MINUTE, "api_per_hour": guard.API_PER_HOUR},
    })

@app.post('/api/admin/guard')
@require_admin_auth
def api_admin_guard_save():
    body = request.get_json(silent=True) or {}
    db.set_admin_setting("guard_enabled", "true" if body.get("enabled", True) else "false")
    allow = str(body.get("allow") or "")[:2000]
    db.set_admin_setting("guard_allow", allow)
    scraper_guard.set_allow(allow)
    return jsonify({"ok": True})

@app.post('/api/admin/guard/unban')
@require_admin_auth
def api_admin_guard_unban():
    body = request.get_json(silent=True) or {}
    ip = str(body.get("ip") or "").strip()
    if not ip or len(ip) > 64:
        return jsonify({"ok": False, "error": "ip"}), 400
    scraper_guard.unban(ip)
    return jsonify({"ok": True})

@app.post('/api/admin/community/claim')
@require_admin_auth
def api_admin_community_claim():
    body = request.get_json(silent=True) or {}
    result = community.claim_station(body.get("code"))
    return jsonify(result), (200 if result.get("ok") else 400)

@app.post('/api/admin/community/backfill')
@require_admin_auth
def api_admin_community_backfill():
    body = request.get_json(silent=True) or {}
    if body.get("action") == "stop":
        return jsonify(community.backfill_stop())
    return jsonify(community.backfill_start())


@app.post('/api/admin/community/update')
@require_admin_auth
def api_admin_community_update():
    if not community.enabled():
        return jsonify({"ok": False, "error": "disabled"}), 400
    result = community.share_now()
    if result.get("ok") and result.get("aggregator") == "sent":
        result["passes"] = community.share_passes(force=True)
    return jsonify(result), (200 if result.get("ok") else 400)

@app.route('/static/<path:filename>')
def static_files(filename):
    """Serve static files with proper headers for JavaScript execution"""
    response = send_file(f'static/{filename}')
    
    # Add specific headers for JavaScript files
    if filename.endswith('.js'):
        response.headers['Content-Type'] = 'application/javascript; charset=utf-8'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    
    return response

@app.route('/sitemap.xml')
def sitemap():
    """The sitemap, with this station's own address."""
    return _station_file('sitemap.xml', 'application/xml')

@app.route('/robots.txt')
def robots():
    """robots.txt, with this station's own address."""
    return _station_file('robots.txt', 'text/plain; charset=utf-8')


# (legacy auth routes removed in favor of DB-backed session auth)

@app.route('/debug/session', methods=['GET'])
@require_admin_auth
def debug_session():
    """Confirm the current admin session without dumping cookie contents."""
    return jsonify({
        'admin_logged_in': True,
        'admin_email': session.get('admin_email'),
    })


# API Management Endpoints
@app.get("/api/configs")
@require_admin_auth
def get_api_configs():
    """Get all API configurations"""
    return jsonify(API_CONFIGS)

@app.post("/api/configs/<api_name>")
@require_admin_auth
def add_or_update_api_config(api_name: str):
    """Add or update an API configuration"""
    try:
        data = request.get_json()
        
        if api_name in API_CONFIGS:
            update_api_config(api_name, **data)
            return jsonify({"message": f"Updated API configuration for {api_name}"})
        else:
            add_api_config(api_name, data)
            return jsonify({"message": f"Added API configuration for {api_name}"})
            
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.get("/api/configs/<api_name>")
@require_admin_auth
def get_api_config_endpoint(api_name: str):
    """Get configuration for a specific API"""
    try:
        config = get_api_config(api_name)
        if config:
            return jsonify(config)
        else:
            logging.warning(f"API {api_name} not found in API_CONFIGS. Available APIs: {list(API_CONFIGS.keys())}")
        return jsonify({"error": f"API {api_name} not found"}), 404
    except Exception as e:
        logging.error(f"Error getting API config for {api_name}: {e}")
        return jsonify({"error": str(e)}), 500

@app.delete("/api/configs/<api_name>")
@require_admin_auth
def delete_api_config(api_name: str):
    """Delete an API configuration"""
    try:
        if api_name in API_CONFIGS:
            del API_CONFIGS[api_name]
            return jsonify({"message": f"Deleted API configuration for {api_name}"})
        else:
            return jsonify({"error": f"API {api_name} not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.post("/api/configs/multi-api")
@require_admin_auth
def save_multi_api_config():
    """Save multi-API configuration"""
    try:
        data = request.get_json()
        
        # Store multi-API configuration in database
        for key, value in data.items():
            db.set_admin_setting(f"multi_api_{key}", str(value))
        
        return jsonify({"message": "Multi-API configuration saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/configs/multi-api")
@require_admin_auth
def get_multi_api_config():
    """Get multi-API configuration"""
    try:
        # Load multi-API configuration from database
        multi_api_config = {}
        db_settings = db.get_all_admin_settings()
        
        for key, value in db_settings.items():
            if key.startswith('multi_api_'):
                config_key = key.replace('multi_api_', '')
                if config_key in ['enable_opensky', 'enable_flightaware']:
                    multi_api_config[config_key] = value.lower() == 'true'
                elif config_key in ['opensky_priority', 'flightaware_priority']:
                    multi_api_config[config_key] = int(value)
                else:
                    multi_api_config[config_key] = value
        
        return jsonify(multi_api_config)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/credentials")
@require_admin_auth
def get_api_credentials():
    """Which API credentials are saved. Values stay on the server."""
    return jsonify({"set": {name: bool(CREDS.get(name)) for name in known_secret_names()}})

@app.post("/api/credentials")
@require_admin_auth
def update_api_credentials():
    """Update API credentials from the admin page."""
    try:
        data = request.get_json(silent=True) or {}
        for key, value in data.items():
            if key in known_secret_names() and value and str(value).strip():
                store_api_secret(key, value)
        if data.get("clientId") or data.get("clientSecret"):
            _token_cache["access_token"] = None
            _token_cache["expires_at"] = 0
        return jsonify({"message": "API credentials updated successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def _feed_catalog_payload():
    settings = db.get_all_admin_settings()
    usage = get_api_usage_stats()
    feeds = []
    for feed in FEEDS:
        config = get_api_config(feed["id"]) or feed["config"]
        usage_row = usage.get(feed["tracking_key"], {})
        feeds.append({
            "id": feed["id"],
            "kind": feed["kind"],
            "name": feed["name"],
            "summary": feed["summary"],
            "docs": feed.get("docs"),
            "fields": [public_field(field, config.get(field["key"], "")) for field in feed["fields"]],
            "secrets": [
                {
                    "key": secret["key"],
                    "label": secret["label"],
                    "help": secret.get("help", ""),
                    "set": bool(CREDS.get(secret["key"])),
                }
                for secret in feed.get("secrets", [])
            ],
            "usage": {
                "daily_requests": usage_row.get("daily_requests") or 0,
                "daily_limit": usage_row.get("daily_limit"),
                "remaining_requests": usage_row.get("remaining_requests"),
                "rate_limit_per_minute": usage_row.get("rate_limit_per_minute"),
                "pricing": usage_row.get("pricing") or feed["usage"].get("pricing"),
                "reset_type": usage_row.get("reset_type"),
                "last_reset": usage_row.get("last_reset"),
            },
        })
    return {
        "active": infer_active(settings),
        "multi": multi_members(settings),
        "feeds": feeds,
    }


@app.get("/api/admin/feeds")
@require_admin_auth
def admin_feeds():
    return jsonify(_feed_catalog_payload())


@app.post("/api/admin/feeds/active")
@require_admin_auth
def admin_feeds_active():
    data = request.get_json(silent=True) or {}
    noise_ids = {feed["id"] for feed in FEEDS if feed["kind"] == "noise"}
    aircraft_ids = {feed["id"] for feed in FEEDS if feed["kind"] == "aircraft"}
    noise = data.get("noise")
    aircraft = data.get("aircraft")
    if noise in noise_ids:
        db.set_admin_setting("active_noise_feed", noise)
    if aircraft == "multi" or aircraft in aircraft_ids:
        db.set_admin_setting("active_aircraft_feed", aircraft)
        db.set_admin_setting("multi_api_multi_api", "true" if aircraft == "multi" else "false")
    known = {feed.get("multi_setting") for feed in FEEDS if feed.get("multi_setting")}
    for row in data.get("multi") or []:
        if not isinstance(row, dict) or row.get("setting") not in known:
            continue
        key = row["setting"]
        if "enabled" in row:
            db.set_admin_setting("multi_api_enable_{0}".format(key), "true" if row["enabled"] else "false")
        if "priority" in row:
            try:
                priority = int(row["priority"])
            except (TypeError, ValueError):
                continue
            db.set_admin_setting("multi_api_{0}_priority".format(key), str(priority))
    return jsonify(_feed_catalog_payload())


@app.post("/api/admin/feeds/<feed_id>")
@require_admin_auth
def admin_feed_save(feed_id):
    feed = feed_by_id(feed_id)
    if not feed:
        return jsonify({"error": "unknown_feed"}), 404
    data = request.get_json(silent=True) or {}
    allowed = {field["key"]: field for field in feed["fields"]}
    updates = {}
    for key, raw in (data.get("fields") or {}).items():
        if key not in allowed:
            continue
        try:
            updates[key] = coerce_field(allowed[key], raw)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_field", "field": key}), 400
    if updates:
        update_api_config(feed_id, **updates)
    changed_secrets = []
    clears = {key for key in (data.get("clear") or []) if isinstance(key, str)}
    for secret in feed.get("secrets", []):
        key = secret["key"]
        value = (data.get("secrets") or {}).get(key)
        if value and str(value).strip():
            store_api_secret(key, value)
            changed_secrets.append(key)
        elif key in clears:
            store_api_secret(key, "")
            changed_secrets.append(key)
    if "clientId" in changed_secrets or "clientSecret" in changed_secrets:
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0
    return jsonify(_feed_catalog_payload())


@app.post("/visitor/settings")
@require_admin_auth
def update_visitor_settings():
    """Update visitor page settings (admin endpoint)"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Update visitor settings
        if "radius_km" in data:
            db.set_admin_setting("visitor_radius_km", str(data["radius_km"]))
        if "altitude_max_m" in data:
            db.set_admin_setting("visitor_altitude_max_m", str(data["altitude_max_m"]))
        if "update_interval_seconds" in data:
            db.set_admin_setting("visitor_update_interval_seconds", str(data["update_interval_seconds"]))
        if "events_view" in data and str(data["events_view"]) in ("zone", "measured", "all"):
            db.set_admin_setting("public_events_view", str(data["events_view"]))
        
        return jsonify({"message": "Visitor settings updated successfully"})
    except Exception as e:
        logging.error(f"Failed to update visitor settings: {e}")
        return jsonify({"error": "Failed to update visitor settings"}), 500

@app.route('/api/admin/restart', methods=['POST'])
@require_admin_auth
def restart_server():
    """Restart the server (admin only)"""
    try:
        import os
        import subprocess
        import sys
        
        # Log the restart request
        logging.info("Server restart requested by admin")
        
        # Under Gunicorn the master process stays up and is told to reload: it
        # starts a fresh worker with the current code and retires this one, with
        # no gap and no second process fighting for the port.
        under_gunicorn = 'gunicorn' in str(request.environ.get('SERVER_SOFTWARE', '')).lower()

        # Return success response first
        response = jsonify({
            'success': True,
            'message': ('Reloading the server. Give it a few seconds and refresh the page.' if under_gunicorn
                        else 'Server restart initiated. Please wait a moment and refresh the page.')
        })
        
        # Schedule restart after response is sent
        def delayed_restart():
            import time
            import signal
            time.sleep(2)  # Give time for response to be sent
            if under_gunicorn:
                try:
                    os.kill(os.getppid(), signal.SIGHUP)
                    logging.info("Gunicorn master asked to reload")
                except Exception as e:
                    logging.error(f"Gunicorn reload failed: {e}")
                return
            
            # Development server: start a new copy, then leave.
            script_path = os.path.abspath(__file__)
            python_path = sys.executable
            
            # Start new server process with current environment
            try:
                env = os.environ.copy()
                subprocess.Popen([python_path, script_path], 
                               cwd=os.path.dirname(script_path),
                               env=env,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                logging.info("New server process started")
            except Exception as e:
                logging.error(f"Failed to start new server process: {e}")
            
            # Terminate current process
            os._exit(0)
        
        import threading
        restart_thread = threading.Thread(target=delayed_restart)
        restart_thread.daemon = True
        restart_thread.start()
        
        return response
        
    except Exception as e:
        logging.error(f"Error during server restart: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# TLS Management API Endpoints
@app.route('/api/tls/status', methods=['GET'])
@require_admin_auth
def tls_status():
    """Get TLS status and certificate information"""
    try:
        tls_config = TLSConfig()
        
        # Check if certificates exist and are valid
        has_certificates = tls_config._has_valid_certificates()
        ssl_context = tls_config.get_ssl_context()
        
        # Get certificate info if available
        cert_info = {}
        if has_certificates:
            try:
                from cryptography import x509
                from datetime import datetime
                with open(tls_config.cert_file, 'rb') as f:
                    cert_data = f.read()
                cert = x509.load_pem_x509_certificate(cert_data)
                cert_info = {
                    'subject': str(cert.subject),
                    'issuer': str(cert.issuer),
                    'not_valid_before': cert.not_valid_before.isoformat(),
                    'not_valid_after': cert.not_valid_after.isoformat(),
                    'days_until_expiry': (cert.not_valid_after - datetime.utcnow()).days
                }
            except Exception as e:
                logging.warning(f"Failed to read certificate info: {e}")
        
        return jsonify({
            'enabled': has_certificates and ssl_context is not None,
            'status': 'active' if has_certificates and ssl_context else 'inactive',
            'message': f"TLS {'enabled' if has_certificates and ssl_context else 'disabled'}",
            'certificate_info': cert_info,
            'cert_file': str(tls_config.cert_file),
            'key_file': str(tls_config.key_file)
        })
        
    except Exception as e:
        logging.error(f"Failed to get TLS status: {e}")
        return jsonify({
            'enabled': False,
            'status': 'error',
            'message': f'TLS status check failed: {str(e)}'
        }), 500

@app.route('/api/tls/generate', methods=['POST'])
@require_admin_auth
def generate_tls_certificate():
    """Generate TLS certificate (admin only)"""
    try:
        data = request.get_json()
        mode = data.get('mode', 'self-signed')
        domain = data.get('domain', 'localhost')
        email = data.get('email', 'admin@localhost')
        
        # Set environment variables for TLS config
        os.environ['TLS_DOMAIN'] = domain
        os.environ['TLS_EMAIL'] = email
        os.environ['TLS_MODE'] = mode
        
        tls_config = TLSConfig()
        
        # Generate certificate based on mode
        if mode == 'self-signed':
            success = tls_config.generate_self_signed_certificate()
            message = "Self-signed certificate generated successfully" if success else "Failed to generate self-signed certificate"
        elif mode == 'letsencrypt':
            success = tls_config.setup_letsencrypt_certificate()
            message = "Let's Encrypt certificate generated successfully" if success else "Failed to generate Let's Encrypt certificate"
        else:  # auto
            success = tls_config.setup_certificates()
            message = "Certificate generated successfully" if success else "Failed to generate certificate"
        
        if success:
            # Save TLS settings to database
            db.set_admin_setting('tls_enabled', 'true')
            db.set_admin_setting('tls_mode', mode)
            db.set_admin_setting('tls_domain', domain)
            db.set_admin_setting('tls_email', email)
            db.set_admin_setting('force_https', 'true')
            
            logging.info(f"TLS certificate generated: {mode} for {domain}")
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'message': message}), 500
            
    except Exception as e:
        logging.error(f"Failed to generate TLS certificate: {e}")
        return jsonify({'success': False, 'message': f'Certificate generation failed: {str(e)}'}), 500

@app.route('/api/tls/test', methods=['GET'])
@require_admin_auth
def test_tls_configuration():
    """Test TLS configuration (admin only)"""
    try:
        tls_config = TLSConfig()
        
        # Test certificate files
        has_certificates = tls_config._has_valid_certificates()
        ssl_context = tls_config.get_ssl_context()
        
        # Test SSL context
        if ssl_context:
            # Test basic SSL functionality
            test_results = []
            
            # Test certificate validity
            if has_certificates:
                test_results.append("✅ Certificates are valid")
            else:
                test_results.append("❌ Certificates are invalid or missing")
            
            # Test SSL context creation
            if ssl_context:
                test_results.append("✅ SSL context created successfully")
            else:
                test_results.append("❌ Failed to create SSL context")
            
            # Test cipher suites
            ciphers = ssl_context.get_ciphers()
            test_results.append(f"✅ {len(ciphers)} cipher suites available")
            
            # Test security headers
            headers = tls_config.get_security_headers()
            test_results.append(f"✅ {len(headers)} security headers configured")
            
            message = "TLS configuration test completed:\n" + "\n".join(test_results)
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'message': 'TLS is not enabled or configured'}), 400
            
    except Exception as e:
        logging.error(f"Failed to test TLS configuration: {e}")
        return jsonify({'success': False, 'message': f'TLS test failed: {str(e)}'}), 500

@app.route('/admin')
def admin_redirect():
    """Redirect to admin login"""
    if session.get('admin_logged_in'):
        return redirect('/admin/dashboard')
    if setup_state() == 'account':
        return redirect('/setup')
    return send_file('admin_login.html')

@app.route('/admin/login')
def admin_login():
    """Serve admin login page"""
    if session.get('admin_logged_in'):
        return redirect('/admin/dashboard')
    # A fresh install has no account yet: the first-run walkthrough creates it.
    if setup_state() == 'account':
        return redirect('/setup')
    return send_file('admin_login.html')

# --- First-run walkthrough -------------------------------------------------
SETUP_STEPS = ("account", "station", "feeds", "alerts", "done")


def setup_state():
    """Which first-run step is owed: account, station, feeds, alerts, skipped or done.

    An install that already has users but never saw the walkthrough counts as done,
    so an upgrade never drops an existing site into the wizard.
    """
    try:
        if db.count_users() == 0:
            return "account"
        state = db.get_admin_setting("setup_state", "") or ""
    except Exception:
        return "done"
    return state if state in ("station", "feeds", "alerts", "skipped", "done") else "done"


def _first_run_owed():
    return setup_state() in ("account", "station", "feeds", "alerts")


@app.before_request
def _send_first_run_to_setup():
    """While the walkthrough is owed, a GET for an admin page goes there.

    Narrow on purpose: never a save, never a data call, never the login page,
    never the sections the admin shell loads in its frame.
    """
    path = request.path
    if request.method != "GET" or not path.startswith("/admin") or path == "/admin/login":
        return None
    if request.args.get("embed") == "1":
        return None
    if not _first_run_owed():
        return None
    return redirect("/setup")


@app.get("/setup")
def setup_page():
    state = setup_state()
    if state == "done":
        return redirect("/admin/dashboard" if session.get("admin_logged_in") else "/admin/login")
    if state != "account" and not _admin_session_ok():
        return redirect("/admin/login")
    response = send_file("setup.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/setup/state")
def setup_state_api():
    return jsonify({"state": setup_state(), "logged_in": _admin_session_ok(), "steps": list(SETUP_STEPS)})


@app.post("/setup/account")
def setup_account():
    """Create the first admin account. Only answers while no account exists."""
    if db.count_users() != 0:
        return jsonify({"error": "account_exists"}), 403
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()[:80]
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"error": "invalid_email"}), 400
    if len(password) < 10:
        return jsonify({"error": "weak_password", "detail": "Use at least 10 characters."}), 400
    if password != str(body.get("confirm") or ""):
        return jsonify({"error": "password_mismatch"}), 400
    first, _, last = name.partition(" ")
    try:
        user_id = db.create_user(email, hash_password(password), first or "Admin", last, is_admin=True, is_active=True)
    except Exception:
        logging.exception("first admin account not created")
        return jsonify({"error": "create_failed"}), 500
    session.clear()
    session["admin_logged_in"] = True
    session["admin_email"] = email
    session["admin_user_id"] = user_id
    session["login_time"] = time.time()
    session["last_activity"] = time.time()
    session.permanent = True
    db.set_admin_setting("setup_state", "station")
    logging.info("First admin account created for %s", email)
    return jsonify({"ok": True, "state": "station"})


@app.post("/api/setup/advance")
@require_admin_auth
def setup_advance():
    body = request.get_json(silent=True) or {}
    to = str(body.get("to") or "")
    if to not in ("station", "feeds", "alerts", "done", "skipped"):
        return jsonify({"error": "unknown_step"}), 400
    db.set_admin_setting("setup_state", to)
    return jsonify({"ok": True, "state": setup_state()})


@app.get("/api/setup/options")
@require_admin_auth
def setup_options():
    """Lists and current values the walkthrough forms start from."""
    flags_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "flags", "4x3")
    try:
        countries = sorted(name[:-4] for name in os.listdir(flags_dir) if name.endswith(".svg") and len(name) == 6)
    except OSError:
        countries = ["nl"]
    settings = db.get_all_admin_settings()
    lat, lon = sensor_position()
    wanted = ("site_place_name", "site_country", "site_timezone", "radius_km", "altitude_max_m",
              "noise_assoc_inner_km", "notify_enabled", "notify_method", "notify_ntfy_url", "notify_email")
    feeds = []
    for feed in FEEDS:
        if feed["kind"] not in ("aircraft", "noise"):
            continue
        config = get_api_config(feed["id"]) or feed["config"]
        feeds.append({
            "id": feed["id"], "kind": feed["kind"], "name": feed["name"], "summary": feed["summary"],
            "docs": feed.get("docs"),
            "sensor_id": config.get("sensor_id") if feed["kind"] == "noise" else None,
            "secrets": [{"key": sec["key"], "label": sec["label"], "help": sec.get("help", ""),
                         "set": bool(CREDS.get(sec["key"]))} for sec in feed.get("secrets", [])],
        })
    return jsonify({
        "state": setup_state(),
        "countries": countries,
        "timezones": list(pytz.common_timezones),
        "values": {key: settings.get(key) for key in wanted},
        "latitude": lat,
        "longitude": lon,
        "feeds": feeds,
        "active": infer_active(settings),
    })


# --- Basic session auth endpoints ---
@app.post('/auth/login')
def auth_login():
    try:
        body = request.get_json(silent=True) or {}
        email = (body.get('email') or '').strip().lower()
        password = body.get('password') or ''
        if not email or not password:
            return jsonify({"error":"missing_credentials"}), 400
        keys = ("ip:" + guard.client_ip(request.headers, request.remote_addr), "email:" + email)
        wait = _login_blocked(keys)
        if wait:
            response = jsonify({"error": "too_many_attempts", "retry_after": wait})
            response.headers["Retry-After"] = str(wait)
            return response, 429
        user = db.get_user_by_email_with_hash(email)
        if not user or not user.get('is_active'):
            _login_failed(keys)
            return jsonify({"error":"invalid_credentials"}), 401
        stored_hash = user.get('password_hash') or ''
        if not verify_password(password, stored_hash):
            _login_failed(keys)
            return jsonify({"error":"invalid_credentials"}), 401
        _login_succeeded(keys)
        if not stored_hash.startswith('pbkdf2$'):
            db.update_user(user['id'], password_hash=hash_password(password))
        # success: set session
        session['admin_logged_in'] = True
        session['admin_email'] = user.get('email')
        session['admin_user_id'] = user.get('id')
        session['login_time'] = time.time()
        session.permanent = True
        return jsonify({"success": True})
    except Exception:
        logging.exception("login failed")
        return jsonify({"error": "login_failed"}), 500

@app.get('/auth/me')
def auth_me():
    return jsonify({
        "user": {
            "email": session.get('admin_email'),
            "id": session.get('admin_user_id'),
            "is_admin": bool(session.get('admin_logged_in'))
        } if session.get('admin_logged_in') else None
    })

@app.post('/auth/logout')
def auth_logout():
    session.clear()
    return jsonify({"success": True})

# ---- forgotten password ------------------------------------------------------
# With a mail relay configured (MAIL_* in the environment), the login page mails a
# link that is good for one hour. Without one, the shell of the box does it:
# scripts/reset_password.py. Nothing in the answer says whether an address exists.

RESET_HINT = "No mail server is set on this station (Admin → Alerts). On the box, run: python3 scripts/reset_password.py you@example.com"


def _mail_configured():
    import notifications
    return notifications.mail_configured()


def _reset_key(token):
    return 'pwreset:' + hashlib.sha256(token.encode()).hexdigest()


def _reset_user_for(token):
    try:
        raw = db.get_admin_setting(_reset_key(token), '') or ''
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        data = None
    if not data or float(data.get('expires', 0)) < time.time():
        return None
    return data.get('user_id')


@app.post('/auth/password-reset')
def auth_password_reset():
    body = request.get_json(silent=True) or {}
    email = (body.get('email') or '').strip().lower()
    if not email:
        return jsonify({"error": "missing_email"}), 400
    if not _mail_configured():
        return jsonify({"error": "reset_unavailable", "message": RESET_HINT}), 501
    keys = ("reset:" + guard.client_ip(request.headers, request.remote_addr),)
    if _login_blocked(keys):
        return jsonify({"error": "too_many_attempts"}), 429
    _login_failed(keys)  # each request counts, so an address cannot be mail-bombed
    user = db.get_user_by_email_with_hash(email)
    if user and user.get('is_active'):
        token = secrets.token_urlsafe(32)
        db.set_admin_setting(_reset_key(token), json.dumps({"user_id": user['id'], "expires": time.time() + 3600}))
        link = public_base_url() + '/auth/reset/' + token
        import notifications
        ok, detail = notifications.send_email(user['email'], "Reset your admin password",
            "Open this link within an hour to set a new password for the Aircraft Noise admin:\n\n" + link +
            "\n\nIf you did not ask for this, ignore this mail; nothing changes.")
        if not ok:
            logging.warning("password reset mail: %s", detail)
    return jsonify({"ok": True, "message": "If that address has an account, a mail with a link is on its way."})


RESET_FORM = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>New password — Aircraft Noise</title><link rel="stylesheet" href="/static/css/theme.css?v=9">
<style>body{margin:0;background:var(--theme-bg-primary,#111);color:var(--theme-text-primary,#eee);font-family:var(--font-sans,system-ui);display:flex;min-height:100vh;align-items:center;justify-content:center}
main{max-width:26rem;padding:2rem;border:1px solid var(--theme-border,#444)}label{display:block;margin:0.8rem 0 0.3rem}input{width:100%;padding:0.6rem;font:inherit;box-sizing:border-box}
button{margin-top:1rem;padding:0.6rem 1rem;font:inherit;cursor:pointer}.err{color:#e07a5f;margin:0.6rem 0 0}</style></head>
<body><main><h1>Set a new password</h1>__BODY__</main></body></html>"""


@app.get('/auth/reset/<token>')
def auth_reset_form(token):
    if not _reset_user_for(token):
        return RESET_FORM.replace('__BODY__', '<p class="err">This link is not valid any more. Ask for a new one from the login page.</p>'), 410
    return RESET_FORM.replace('__BODY__', '<form method="post"><label>New password<input type="password" name="password" minlength="10" required autocomplete="new-password"></label>'
                              '<label>Again<input type="password" name="confirm" minlength="10" required autocomplete="new-password"></label>'
                              '<button type="submit">Change the password</button></form><p class="small">At least 10 characters.</p>')


@app.post('/auth/reset/<token>')
def auth_reset_set(token):
    user_id = _reset_user_for(token)
    if not user_id:
        return RESET_FORM.replace('__BODY__', '<p class="err">This link is not valid any more. Ask for a new one from the login page.</p>'), 410
    password = request.form.get('password') or ''
    if len(password) < 10 or password != (request.form.get('confirm') or ''):
        return RESET_FORM.replace('__BODY__', '<p class="err">The two passwords must match and have 10 characters or more. <a href="">Try again</a>.</p>'), 400
    db.update_user(int(user_id), password_hash=hash_password(password))
    db.set_admin_setting(_reset_key(token), '')
    return redirect('/admin/login?reset=1')

def send_admin_page(filename):
    """Serve the shared admin shell, or one section when the shell asks for it."""
    if request.args.get('embed') != '1':
        response = send_file('admin_shell.html')
        response.headers['Cache-Control'] = 'no-cache'
        return response
    try:
        with open(filename, encoding='utf-8') as handle:
            html = handle.read()
    except OSError:
        logging.error("Failed to serve %s", filename)
        return "Not Found", 404
    html = html.replace('<html ', '<html class="is-embed" ', 1)
    response = Response(html, mimetype='text/html; charset=utf-8')
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route('/admin/dashboard')
@require_admin_auth
def admin_dashboard():
    """Serve admin dashboard"""
    return send_admin_page('admin_dashboard.html')

@app.route('/admin/database')
@require_admin_auth
def admin_database():
    """Serve admin database management page"""
    return send_admin_page('admin_database.html')

@app.route('/admin/settings')
@require_admin_auth
def admin_settings():
    """Serve admin settings page"""
    return send_admin_page('admin_settings.html')

@app.route('/admin/detection-zone')
@require_admin_auth
def admin_detection_zone():
    """Serve custom detection zone editor"""
    return send_admin_page('admin_detection_zone.html')

@app.route('/admin/users')
@require_admin_auth
def admin_users():
    """Serve admin user management page"""
    return send_admin_page('admin_users.html')

@app.route('/admin/api-usage')
@require_admin_auth
def admin_api_usage():
    """Serve admin API usage monitoring page"""
    return send_admin_page('admin_api_usage.html')

# --- User CRUD (admin) ---
@app.get('/api/admin/users')
@require_admin_auth
def api_list_users():
    try:
        return jsonify({"users": db.list_users()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post('/api/admin/users')
@require_admin_auth
def api_create_user():
    try:
        data = request.get_json() or {}
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        first_name = data.get('first_name') or ''
        last_name = data.get('last_name') or ''
        is_admin = bool(data.get('is_admin'))
        if not email or not password:
            return jsonify({"error": "email and password required"}), 400
        pw_hash = hash_password(password)
        uid = db.create_user(email, pw_hash, first_name, last_name, is_admin=is_admin, is_active=True)
        return jsonify({"id": uid, "message": "created"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.patch('/api/admin/users/<int:user_id>')
@require_admin_auth
def api_update_user(user_id: int):
    try:
        data = request.get_json() or {}
        fields = {}
        for k in ('email','first_name','last_name'):
            if k in data: fields[k] = data[k]
        if 'password' in data and data['password']:
            fields['password_hash'] = hash_password(data['password'])
        if 'is_admin' in data: fields['is_admin'] = bool(data['is_admin'])
        if 'is_active' in data: fields['is_active'] = bool(data['is_active'])
        ok = db.update_user(user_id, **fields)
        if not ok:
            return jsonify({"error": "no changes"}), 400
        return jsonify({"message": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.delete('/api/admin/users/<int:user_id>')
@require_admin_auth
def api_delete_user(user_id: int):
    try:
        ok = db.delete_user(user_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify({"message": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get('/api/admin/logs')
@require_admin_auth
def api_admin_logs():
    """Recent process log lines for the admin logs page."""
    try:
        limit = request.args.get('limit', 200, type=int) or 200
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 400))
    level = (request.args.get('level') or 'all').lower()
    source = (request.args.get('source') or 'all').lower()
    rows = RING_LOGS.snapshot()
    counts = {}
    for row in rows:
        counts[row['level']] = counts.get(row['level'], 0) + 1
    if level != 'all':
        rows = [row for row in rows if row['level'] == level]
    if source != 'all':
        rows = [row for row in rows if row['source'] == source]
    rows = list(reversed(rows[-limit:]))
    return jsonify({"logs": rows, "counts": counts, "total": sum(counts.values())})

@app.route('/admin/logs')
@require_admin_auth
def admin_logs():
    """Serve admin logs page"""
    return send_admin_page('admin_logs.html')



# Aircraft API routes
@app.route('/api/events', methods=['GET'])
def get_aircraft_events():
    """Get recent aircraft events"""
    try:
        # Get query parameters
        limit = max(1, min(request.args.get('limit', 50, type=int) or 50, 500))
        hours = max(1, min(request.args.get('hours', 24, type=int) or 24, 24 * 31))
        
        # Query aircraft events from database
        conn = sqlite3.connect(db.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT callsign, icao24, distance_km, altitude_m, speed_kmh, 
                   heading_deg, created_at, type
            FROM aircraft_events 
            WHERE created_at >= datetime('now', '-{} hours')
            ORDER BY created_at DESC 
            LIMIT ?
        '''.format(hours), (limit,))
        
        events = []
        for row in cursor.fetchall():
            events.append({
                'callsign': row[0],
                'icao24': row[1],
                'distance_km': row[2],
                'altitude_m': row[3],
                'speed_kmh': row[4],
                'heading_deg': row[5],
                'created_at': row[6],
                'type': row[7]
            })
        
        conn.close()
        return jsonify(events)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/visitor/aircraft', methods=['GET'])
def visitor_aircraft():
    """Get aircraft data for visitor tracking"""
    sensor_lat, sensor_lon = sensor_position()
    try:
        # Get current aircraft from OpenSky API
        aircraft_config = get_api_config('aircraft')
        if aircraft_config and aircraft_config.get('multi_api'):
            aircraft_data = get_aircraft_data_multi_api()
        else:
            aircraft_data = get_aircraft_data_opensky()
        
        if not aircraft_data:
            return jsonify([])
        
        # Get current settings
        settings = db.get_all_admin_settings()
        radius_km = float(settings.get('radius_km', 8.0))
        alt_max_m = float(settings.get('altitude_max_m', 2000.0))
        
        # Filter aircraft within range
        try:
            # Debug: log sample of fetched callsigns
            sample_raw = []
            for s in aircraft_data[:8]:
                if isinstance(s, dict):
                    sample_raw.append((s.get('callsign') or '').strip())
                elif isinstance(s, list) and len(s) > 1:
                    sample_raw.append((s[1] or '').strip())
            logging.info("visitor_aircraft: fetched=%s sample_callsigns=%s", len(aircraft_data), [c for c in sample_raw if c])
        except Exception:
            pass
        filtered_aircraft = []
        for aircraft in aircraft_data:
            if isinstance(aircraft, dict):
                # Multi-API format
                distance = aircraft.get('distance_km', 0)
                altitude = aircraft.get('altitude_m', 0)
                # Fill from raw if missing
                if not distance and aircraft.get('latitude') is not None and aircraft.get('longitude') is not None:
                    distance = calculate_distance(
                        sensor_lat, sensor_lon,
                        aircraft.get('latitude'), aircraft.get('longitude')
                    )
                if not altitude:
                    alt_g = aircraft.get('geo_altitude')
                    alt_b = aircraft.get('baro_altitude')
                    altitude = alt_g if alt_g is not None else alt_b or 0
            else:
                # OpenSky format
                distance = calculate_distance(
                    sensor_lat, sensor_lon,
                    aircraft[6], aircraft[5]
                )
                altitude = aircraft[7] or aircraft[13] or 0
            
            if distance <= radius_km and altitude <= alt_max_m:
                if isinstance(aircraft, dict):
                    filtered_aircraft.append(aircraft)
                else:
                    # Convert OpenSky format to dict
                    filtered_aircraft.append({
                        'icao24': aircraft[0],
                        'callsign': aircraft[1].strip() if aircraft[1] else '',
                        'origin_country': aircraft[2],
                        'time_position': aircraft[3],
                        'last_contact': aircraft[4],
                        'longitude': aircraft[5],
                        'latitude': aircraft[6],
                        'baro_altitude': aircraft[7],
                        'on_ground': aircraft[8],
                        'velocity': aircraft[9],
                        'true_track': aircraft[10],
                        'vertical_rate': aircraft[11],
                        'sensors': aircraft[12],
                        'geo_altitude': aircraft[13],
                        'squawk': aircraft[14],
                        'spi': aircraft[15],
                        'position_source': aircraft[16],
                        'distance_km': distance,
                        'altitude_m': altitude
                    })
        try:
            # Debug: log filtered sample
            logging.info("visitor_aircraft: filtered=%s sample=%s", len(filtered_aircraft), [(a.get('callsign'), round(a.get('distance_km', 0), 2)) for a in filtered_aircraft[:5] if isinstance(a, dict)])
        except Exception:
            pass

        return jsonify(filtered_aircraft)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/aircraft', methods=['GET'])
def api_aircraft():
    """Get current aircraft data (alias for visitor/aircraft)"""
    return visitor_aircraft()

# Helper functions for aircraft data
def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two points in kilometers using Haversine formula"""
    import math
    
    # Convert to radians
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    
    # Haversine formula
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    # Radius of earth in kilometers
    r = 6371
    return c * r

def get_aircraft_data_opensky():
    """OpenSky states for the sensor box. Uses the saved client when one is stored."""
    feed_id = "aircraft"
    try:
        settings = db.get_all_admin_settings()
        radius_km = float(settings.get('radius_km', 8.0))
        lamin, lomin, lamax, lomax = _sensor_bbox(radius_km)
        url = (
            "https://opensky-network.org/api/states/all"
            f"?lamin={lamin}&lomin={lomin}&lamax={lamax}&lomax={lomax}"
        )
        response = _opensky_get(url)
        if response.status_code == 401:
            _token_cache["access_token"] = None
            _token_cache["expires_at"] = 0
            response = _opensky_get(url)
        track_api_usage('aircraft', response.headers)
        if response.status_code != 200:
            logging.warning("OpenSky API returned status %s", response.status_code)
            _mark_feed(feed_id, True)
            return []
        states_list = (response.json() or {}).get('states') or []
        aircraft_data = []
        for state in states_list:
            if isinstance(state, list) and len(state) >= 17:
                aircraft_data.append({
                    'icao24': state[0],
                    'callsign': state[1].strip() if state[1] else '',
                    'origin_country': state[2],
                    'time_position': state[3],
                    'last_contact': state[4],
                    'longitude': state[5],
                    'latitude': state[6],
                    'baro_altitude': state[7],
                    'on_ground': state[8],
                    'velocity': state[9],
                    'true_track': state[10],
                    'vertical_rate': state[11],
                    'sensors': state[12],
                    'geo_altitude': state[13],
                    'squawk': state[14],
                    'spi': state[15],
                    'position_source': state[16],
                    'source': 'opensky'
                })
        _mark_feed(feed_id, False)
        logging.info("OpenSky returned %s aircraft", len(aircraft_data))
        return aircraft_data
    except Exception as e:
        logging.error("Error getting OpenSky aircraft data: %s", e)
        _mark_feed(feed_id, True)
        return []


def _opensky_get(url):
    headers, mode = get_auth_headers()
    headers = dict(headers)
    headers['User-Agent'] = 'plane-noise-dashboard/1.0'
    auth = None
    if mode == "basic":
        auth = (CREDS.get("username"), CREDS.get("password"))
    return requests.get(url, headers=headers, auth=auth, timeout=20)

def _feet_to_metres(value):
    """readsb altitude field: feet as a number, or the string 'ground'."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) * 0.3048
    if isinstance(value, str) and value.strip().lower() == 'ground':
        return 0.0
    return None


def speed_kmh_from_feed(vel, source):
    """Feed speed as whole km/h. OpenSky reports m/s; FlightAware and Airplanes.live report knots."""
    if vel is None:
        return None
    try:
        v = float(vel)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if source == 'opensky':
        return int(round(v * 3.6))
    if source in ('flightaware', 'airplanes_live', 'adsb_fi', 'adsb_lol', 'hub'):
        return int(round(v * 1.852))
    # Unknown feed: small numbers are m/s, larger ones are already km/h.
    return int(round(v * 3.6)) if v < 200 else int(round(v))


def _readsb_point_feed(feed_id, source, default_base, auth_header=None, blur_deg=0.0, query=''):
    """Aircraft around the sensor from a readsb-style /point API: airplanes.live,
    adsb.fi and adsb.lol all answer in the ADS-B Exchange v2 shape (feet, knots).
    With blur_deg the asked position is rounded to that grid and the circle
    widened to cover the difference, so the other side never learns the exact
    microphone; the zone test afterwards uses the real one."""
    try:
        settings = db.get_all_admin_settings()
        radius_km = float(settings.get('radius_km', 8.0))
        lat, lon = sensor_position()
        if blur_deg:
            lat, lon = round(lat / blur_deg) * blur_deg, round(lon / blur_deg) * blur_deg
            radius_km += blur_deg * 111.0  # the rounding can move the centre by up to half a cell each way
        radius_nm = max(1, int(math.ceil(radius_km / 1.852)))  # these APIs take nautical miles
        cfg = get_api_config(feed_id) or {}
        base_url = (cfg.get('base_url') or default_base).rstrip('/')
        template = (cfg.get('endpoints') or {}).get('point') or '/point/{lat}/{lon}/{radius}'
        url = base_url + template.format(lat=lat, lon=lon, radius=radius_nm) + query
        headers = {'User-Agent': 'plane-noise-dashboard/1.0 (+https://aircraftnoise.eu)'}
        if auth_header:
            headers.update(auth_header)
        r = requests.get(url, headers=headers, timeout=int(cfg.get('timeout') or 20))
        track_api_usage(feed_id, r.headers)

        if r.status_code != 200:
            logging.warning("%s status=%s body=%s", feed_id, r.status_code, r.text[:200])
            _mark_feed(feed_id, True)
            return []
        j = r.json() or {}
        states = j.get('ac') or j.get('aircraft') or []
        aircraft_data = []
        for a in states:
            try:
                baro_m = _feet_to_metres(a.get('alt_baro'))
                geom_m = _feet_to_metres(a.get('alt_geom'))
                aircraft_data.append({
                    'icao24': (a.get('hex') or '').lower(),
                    'callsign': (a.get('flight') or a.get('callsign') or '').strip(),
                    'longitude': a.get('lon'),
                    'latitude': a.get('lat'),
                    'geo_altitude': geom_m if geom_m is not None else baro_m,
                    'baro_altitude': baro_m,
                    'velocity': a.get('gs') or a.get('spd'),
                    'true_track': a.get('track') or a.get('truetrack') or a.get('heading'),
                    'on_ground': a.get('ground') or a.get('onground') or False,
                    'source': source
                })
            except Exception:
                continue
        _mark_feed(feed_id, False)
        return aircraft_data
    except Exception as e:
        logging.error("Error getting %s data: %s", feed_id, e)
        _mark_feed(feed_id, True)
        return []


def get_aircraft_data_airplanes_live():
    """Airplanes.live /point around the sensor. An optional key goes in the auth header."""
    key = CREDS.get("airplanes_live_key")
    return _readsb_point_feed('aircraft_airplanes_live', 'airplanes_live', 'https://api.airplanes.live/v2',
                              {"auth": key} if key else None)


def get_aircraft_data_hub():
    """The data service's feed hub: one fetch per region, shared. Authenticates
    with the station id and key the community sharing already has."""
    station = str(db.get_admin_setting('community_last_id', '') or '')
    station_key = str(db.get_admin_setting('api_secret.community_station_key', '') or '')
    if not station or not station_key:
        logging.info("Hub feed: share the station first (Settings → Community), the hub uses its id and key")
        _mark_feed('aircraft_hub', True)
        return []
    return _readsb_point_feed('aircraft_hub', 'hub', 'https://data.aircraftnoise.eu/api/v1/hub',
                              {"X-Station-Key": station_key}, blur_deg=0.02, query='?station=' + station)


def get_aircraft_data_adsb_fi():
    """adsb.fi open data, v3 radius endpoint. One request a second, credit in the footer."""
    return _readsb_point_feed('aircraft_adsb_fi', 'adsb_fi', 'https://opendata.adsb.fi/api/v3')


def get_aircraft_data_adsb_lol():
    """adsb.lol public API. 500 requests a day per address, so a slow fallback only."""
    return _readsb_point_feed('aircraft_adsb_lol', 'adsb_lol', 'https://api.adsb.lol/v2')


AIRCRAFT_FETCHERS.update({
    'opensky': get_aircraft_data_opensky,
    'airplanes_live': get_aircraft_data_airplanes_live,
    'adsb_fi': get_aircraft_data_adsb_fi,
    'hub': get_aircraft_data_hub,
    'adsb_lol': get_aircraft_data_adsb_lol,
    'flightaware': get_flightaware_aircraft_data,
})


def _active_poll_intervals():
    settings = db.get_all_admin_settings()
    active = infer_active(settings)
    noise_cfg = get_api_config(active['noise']) or {}
    noise_interval = int(noise_cfg.get('poll_interval_seconds', DEFAULT_NOISE_POLL_INTERVAL))
    if active['aircraft'] == 'multi':
        members = [row for row in multi_members(settings) if row['enabled']]
        # The first enabled feed sets the pace. A faster fallback must not
        # pull OpenSky down to a 10 second poll.
        lead = members[0] if members else None
        cfg = get_api_config(lead['id']) if lead else None
        aircraft_interval = int((cfg or {}).get('poll_interval_seconds', DEFAULT_AIRCRAFT_POLL_INTERVAL))
        # OpenSky's pace follows the quiet-hours schedule, never faster than the
        # floor; a fallback feed must not pull it down to a 10 second poll.
        if lead and lead['id'] == 'aircraft':
            aircraft_interval = max(aircraft_interval, opensky_interval_now(settings))
    else:
        aircraft_cfg = get_api_config(active['aircraft']) or {}
        aircraft_interval = int(aircraft_cfg.get('poll_interval_seconds', DEFAULT_AIRCRAFT_POLL_INTERVAL))
        if active['aircraft'] == 'aircraft':
            aircraft_interval = max(aircraft_interval, opensky_interval_now(settings))
    return max(1, aircraft_interval), max(1, noise_interval)


# Background polling functions
def background_polling():
    """Background thread that polls aircraft and noise on independent schedules"""
    logging.info("Starting background polling thread")
    last_aircraft_poll = 0.0
    last_noise_poll = 0.0

    global _last_poll_tick
    while True:
        try:
            now = time.time()
            _last_poll_tick = now

            aircraft_interval, noise_interval = _active_poll_intervals()

            # Poll aircraft if due
            if now - last_aircraft_poll >= aircraft_interval:
                try:
                    poll_aircraft_data()
                finally:
                    last_aircraft_poll = time.time()

            # Poll noise if due
            now2 = time.time()
            if now2 - last_noise_poll >= noise_interval:
                try:
                    poll_noise_data()
                finally:
                    last_noise_poll = time.time()

            # Sleep until the next task is due (at least 1s)
            next_aircraft_due = (last_aircraft_poll + aircraft_interval) - time.time()
            next_noise_due = (last_noise_poll + noise_interval) - time.time()
            sleep_for = max(1.0, min(next_aircraft_due, next_noise_due))
            time.sleep(sleep_for)

        except Exception as e:
            logging.error(f"Error in background polling: {e}")
            time.sleep(5)

def poll_aircraft_data():
    """Poll aircraft data from configured APIs and process it"""
    try:
        # Get current settings from database
        settings = db.get_all_admin_settings()
        # Accept both legacy and new keys
        radius_km = float(
            settings.get('radius_km')
            or settings.get('detection_radius_km')
            or 8.0
        )
        alt_max_m = float(
            settings.get('altitude_max_m')
            or settings.get('max_altitude_m')
            or 2000.0
        )
        
        active = infer_active(settings)['aircraft']
        if active == 'multi':
            aircraft_data = get_aircraft_data_multi_api()
            label = 'multi'
        else:
            feed = feed_by_id(active)
            fetcher = AIRCRAFT_FETCHERS.get(feed['fetcher']) if feed else None
            if not fetcher:
                logging.warning("No aircraft fetcher registered for %s", active)
                return
            aircraft_data = fetcher()
            label = active
        count = len(aircraft_data) if aircraft_data else 0
        logging.info("poll_aircraft_data: %s states=%s, radius_km=%s, alt_max_m=%s", label, count, radius_km, alt_max_m)
        global _last_aircraft_ok, _last_aircraft_error
        if active == 'multi':
            tried = [row['id'] for row in multi_members(settings) if row['enabled']]
            feed_down = bool(tried) and all(_feed_failed.get(feed_id, False) for feed_id in tried)
        else:
            feed_down = _feed_failed.get(active, False)
        if not feed_down:
            _last_aircraft_ok = time.time()
            _last_aircraft_error = None
        elif _last_aircraft_error is None:
            _last_aircraft_error = f"{label} feed marked failed"
        if aircraft_data:
            process_aircraft_states(aircraft_data, radius_km, alt_max_m)
        else:
            logging.info("No aircraft data from %s", label)
        run_housekeeping()
            
    except Exception as e:
        _last_aircraft_error = str(e)[:200]
        logging.error(f"Error polling aircraft data: {e}")

def _admin_float(key, default):
    raw = db.get_admin_setting(key, None)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _weather_sensor_point():
    """Sensor used for the station reading. Not the detection-zone center."""
    return _admin_float("sensor_lat", 0.0), _admin_float("sensor_lon", 0.0)


def _public_map_zoom():
    """The public map's zoom level, or None for automatic (fit the zone)."""
    raw = str(db.get_admin_setting("public_map_zoom", "") or "").strip()
    try:
        z = int(float(raw))
    except (TypeError, ValueError):
        return None
    return z if 5 <= z <= 15 else None


def _weather_advice_enabled():
    """Weather notes stay on until an admin turns the setting off."""
    raw = db.get_admin_setting("weather_advice_enabled", "true")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _rain_flag(rain):
    """1 when rain.1h is present and above zero. A missing rain object is not falling."""
    if not isinstance(rain, dict) or "1h" not in rain:
        return 0
    try:
        return 1 if float(rain["1h"]) > 0 else 0
    except (TypeError, ValueError):
        return 0


def _surface_fields(body):
    if not isinstance(body, dict):
        return None
    main = body.get("main")
    wind = body.get("wind")
    if not isinstance(main, dict) or not isinstance(wind, dict):
        return None
    if "temp" not in main or "speed" not in wind or "deg" not in wind:
        return None
    try:
        return {
            "wx_wind_speed": float(wind["speed"]),
            "wx_wind_from_deg": float(wind["deg"]),
            "wx_rain": _rain_flag(body.get("rain")),
            "wx_temp_c": float(main["temp"]),
        }
    except (TypeError, ValueError):
        return None


def _read_openweather_surface(lat, lon):
    if not str(CREDS.get("openweather_key") or "").strip():
        return None
    response = make_api_request(
        "weather",
        "current",
        params={"lat": lat, "lon": lon},
    )
    if response is None or getattr(response, "status_code", None) != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return _surface_fields(body)


def _read_open_meteo_hourly(lat, lon):
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,temperature_80m",
            "timezone": _place_timezone_name(),
        },
        timeout=10,
        headers={"User-Agent": "plane-noise-dashboard/1.0"},
    )
    if response.status_code != 200:
        logging.warning("Open-Meteo station snapshot failed")
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("hourly"), dict):
        return None
    track_api_usage("open_meteo", response.headers)
    return body


def _parse_hourly_label(label, tz):
    text = str(label).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) >= 16 and text[13] == ":" and (len(text) == 16 or text[16] in "+-"):
        text = text[:16] + ":00" + text[16:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        try:
            return tz.localize(parsed, is_dst=None)
        except Exception:
            return None
    return parsed.astimezone(tz)


def _height_pair(hourly_body, stamp):
    """Temperatures for the hour that contains stamp. Not the first hour in the payload."""
    if not isinstance(hourly_body, dict) or stamp is None:
        return None
    hourly = hourly_body.get("hourly")
    if not isinstance(hourly, dict):
        return None
    times = hourly.get("time") or []
    temps_2m = hourly.get("temperature_2m") or []
    temps_80m = hourly.get("temperature_80m") or []
    tz = get_sensor_timezone()
    try:
        local = datetime.fromtimestamp(float(stamp), timezone.utc).astimezone(tz)
    except (TypeError, ValueError, OSError):
        return None
    target = (local.year, local.month, local.day, local.hour)
    for index, label in enumerate(times):
        parsed = _parse_hourly_label(label, tz)
        if parsed is None:
            continue
        if (parsed.year, parsed.month, parsed.day, parsed.hour) != target:
            continue
        if index >= len(temps_2m) or index >= len(temps_80m):
            return None
        if temps_2m[index] is None or temps_80m[index] is None:
            return None
        try:
            return float(temps_2m[index]), float(temps_80m[index])
        except (TypeError, ValueError):
            return None
    return None


def take_station_snapshot():
    """Surface reading plus the cached hourly temperatures. At most once per five minutes.

    A failed OpenWeatherMap attempt is remembered for the same window, so the next
    aircraft does not call again. The chosen hour is not cached; callers pick it
    from the hourly JSON for each stamp.
    """
    now = time.time()
    cache = _station_snapshot_cache
    fetched = cache.get("at")
    if fetched is not None and now - fetched < STATION_SNAPSHOT_SECONDS:
        return cache
    lat, lon = _weather_sensor_point()
    surface = None
    hourly = None
    try:
        surface = _read_openweather_surface(lat, lon)
    except requests.RequestException:
        logging.warning("Station weather snapshot failed")
        surface = None
    if surface is not None:
        try:
            hourly = _read_open_meteo_hourly(lat, lon)
        except requests.RequestException:
            logging.warning("Open-Meteo station snapshot failed")
            hourly = None
    cache["at"] = now
    cache["surface"] = surface
    cache["hourly"] = hourly
    return cache


def weather_for_aircraft(snapshot, stamp, lat, lon):
    """Weather fields for the aircraft sample, or nothing when the surface reading is missing."""
    surface = snapshot.get("surface") if isinstance(snapshot, dict) else None
    if not surface or lat is None or lon is None:
        return {}
    pair = _height_pair(snapshot.get("hourly"), stamp)
    return {
        "wx_wind_speed": surface.get("wx_wind_speed"),
        "wx_wind_from_deg": surface.get("wx_wind_from_deg"),
        "wx_rain": surface.get("wx_rain"),
        "wx_temp_c": surface.get("wx_temp_c"),
        "wx_temp_2m_c": None if pair is None else pair[0],
        "wx_temp_80m_c": None if pair is None else pair[1],
        "wx_lat": float(lat),
        "wx_lon": float(lon),
    }


# English wording selected by the ids /recent_events sends. The payload carries the ids only.
WEATHER_SENTENCE_TEXT = {
    "R7": (
        "The wind was blowing from the aircraft toward the sensor. "
        "Downwind, the sound bends downward and is heard more strongly than in calm air."
    ),
    "R8": (
        "The wind was blowing from the sensor toward the aircraft. "
        "Upwind, the sound bends upward, so the aircraft is heard more weakly than in calm air."
    ),
    "R9": (
        "The wind was strong. Strong wind raises the noise around the microphone "
        "and masks the aircraft, and it makes the level unsteady."
    ),
    "R10": (
        "Rain was falling. The measured level includes the rain, so it is not a clean "
        "aircraft level, and the rain can mask the pass."
    ),
    "R11": (
        "The model shows warmer air at 80 m than at the ground. That low inversion "
        "bends sound back down, so the aircraft carries farther than it would in a "
        "normal daytime atmosphere."
    ),
    "R12": (
        "The air was calm and cold. Sound often carries farther in that kind of air. "
        "The model does not show a warmer layer at 80 m."
    ),
}


def _stored_float(event, key):
    value = event.get(key) if isinstance(event, dict) else None
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _angle_apart(left, right):
    """Shortest arc between two compass directions, in degrees."""
    return abs((left - right + 180.0) % 360.0 - 180.0)


def _initial_bearing_deg(lat1, lon1, lat2, lon2):
    """Initial bearing from point 1 to point 2. None when the points coincide."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return None
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def classify_weather_sentences(event, sensor=None):
    """Ids R7–R12 for one stored row. Nothing is written back onto the row."""
    if not isinstance(event, dict):
        return []
    ids = []
    wind_from = _stored_float(event, "wx_wind_from_deg")
    wx_lat = _stored_float(event, "wx_lat")
    wx_lon = _stored_float(event, "wx_lon")
    if wind_from is not None and wx_lat is not None and wx_lon is not None:
        if sensor is None:
            sensor_lat, sensor_lon = _weather_sensor_point()
        else:
            sensor_lat, sensor_lon = sensor
        travel = (wind_from + 180.0) % 360.0
        toward_sensor = _initial_bearing_deg(wx_lat, wx_lon, sensor_lat, sensor_lon)
        toward_aircraft = _initial_bearing_deg(sensor_lat, sensor_lon, wx_lat, wx_lon)
        if toward_sensor is not None and _angle_apart(travel, toward_sensor) <= 60.0:
            ids.append("R7")
        if toward_aircraft is not None and _angle_apart(travel, toward_aircraft) <= 60.0:
            ids.append("R8")
    speed = _stored_float(event, "wx_wind_speed")
    if speed is not None and speed >= 5.0:
        ids.append("R9")
    if event.get("wx_rain") == 1:
        ids.append("R10")
    temp_2m = _stored_float(event, "wx_temp_2m_c")
    temp_80m = _stored_float(event, "wx_temp_80m_c")
    ground = _stored_float(event, "wx_temp_c")
    heights = temp_2m is not None and temp_80m is not None
    inversion = heights and temp_80m > temp_2m
    if inversion:
        ids.append("R11")
    if (
        heights
        and not inversion
        and speed is not None
        and speed < 2.0
        and ground is not None
        and ground <= 5.0
    ):
        ids.append("R12")
    return ids


def process_aircraft_states(states, radius_km, alt_max_m):
    """Process aircraft states (OpenSky arrays or merged dicts) and log events."""
    try:
        if not states:
            return

        station_snapshot = None
        if _weather_advice_enabled():
            try:
                station_snapshot = take_station_snapshot()
            except Exception:
                logging.error("Station snapshot failed")
                station_snapshot = None

        inrange_count = 0
        snapshot_aircraft = []
        zone_test = _zone_membership_test()
        for state in states:
            # Support both list/tuple (OpenSky states) and dict (merged multi-API)
            if isinstance(state, dict):
                icao24 = (state.get('icao24') or '').strip().lower()
                callsign = (state.get('callsign') or '').strip()
                lon = state.get('longitude')
                lat = state.get('latitude')
                # prefer geo_altitude, fall back to baro
                alt = state.get('geo_altitude') if state.get('geo_altitude') is not None else state.get('baro_altitude')
                vel = state.get('velocity')
                hdg = state.get('true_track')
                source = state.get('source')
            else:
                # Assume OpenSky state vector list
                if len(state) < 14:
                    continue
                icao24 = (state[0] or '').strip().lower()
                callsign = (state[1] or '').strip()
                lon = state[5]
                lat = state[6]
                alt = state[7] or state[13]
                vel = state[9]
                hdg = state[10]
                source = 'opensky'
            
            if lat is None or lon is None:
                continue
                
            speed_kmh_val = speed_kmh_from_feed(vel, source)

            # Distances from the same sensor position the zone check uses
            sensor_lat, sensor_lon = sensor_position()
            dist = haversine_km(sensor_lat, sensor_lon, lat, lon)
            r3d = slant_range_km(dist, alt)
            
            # Determine in-range conditions using custom detection zone
            in_radius = is_aircraft_in_detection_zone(lat, lon, alt, alt_max_m)
            in_altitude = (alt is None or alt <= alt_max_m)
            snapshot_aircraft.append({
                'icao24': icao24,
                'callsign': (callsign or '').upper().strip(),
                'latitude': lat,
                'longitude': lon,
                'altitude_m': int(alt) if alt is not None else None,
                'speed_kmh': speed_kmh_val,
                'heading_deg': int(hdg) if hdg is not None else None,
                'distance_km': round(dist, 3),
                'slant_km': round(r3d, 3) if r3d is not None else None,
                'in_zone': bool(in_radius and in_altitude),
                'in_noise_range': bool(in_radius and in_altitude and noise_association_allowed(dist, alt)),
            })

            # Log/update a 'pass' only when within radius AND under altitude threshold
            if in_radius and in_altitude:
                inrange_count += 1
                # Use aircraft last contact time for better correlation when available
                if isinstance(state, dict):
                    last_contact = state.get('last_contact')
                else:
                    last_contact = state[4] if len(state) > 4 else None
                # Use the aircraft state timestamp for correlation and logging for better sync with noise samples
                state_time_sec = float(last_contact or time.time())
                slant_now = r3d if r3d is not None else dist
                remember_track_point(icao24, state_time_sec, slant_now)
                in_zone_now = bool(zone_test(lat, lon, dist))
                try:
                    db.add_track_sample(
                        ts=state_time_sec, icao24=icao24, callsign=(callsign or '').upper().strip() or None,
                        latitude=lat, longitude=lon, altitude_m=int(alt) if alt is not None else None,
                        distance_km=dist, slant_km=r3d, heading_deg=int(hdg) if hdg is not None else None,
                    )
                except Exception as e:
                    logging.warning(f"Track sample not stored: {e}")

                # Assign the decibel only when the 3D slant range is within the inner
                # radius, and only when no closer aircraft holds that peak.
                noise_pick = None
                if noise_association_allowed(dist, alt):
                    noise_pick = claim_noise_peak(icao24, state_time_sec, slant_now, band_s=45.0)
                noise_associated = noise_pick[0] if noise_pick else None
                noise_peak_ts = noise_pick[1] if noise_pick else None
                noise_peak_max = noise_la_max_at(noise_peak_ts)

                # Enrich with aircraft metadata and destination
                meta = _get_aircraft_meta(icao24)
                # Normalize callsign (uppercase, trimmed)
                callsign_norm = callsign.upper().strip() if callsign else ''
                identity = _flight_identity(icao24, callsign_norm, meta)
                destination = identity["destination"]

                # Upsert pass event within recent window
                recent_pass = db.get_recent_pass_for_aircraft(icao24, within_seconds=900)
                sample_weather = weather_for_aircraft(station_snapshot, state_time_sec, lat, lon)
                if recent_pass:
                    db.update_pass_metrics(
                        recent_pass['id'],
                        distance_km=dist,
                        spl_db=noise_associated,
                        spl_peak_ts=noise_peak_ts,
                        spl_max_db=noise_peak_max,
                        in_zone=in_zone_now,
                        altitude_m=int(alt) if alt else None,
                        speed_kmh=speed_kmh_val,
                        heading_deg=int(hdg) if hdg else None,
                        callsign=callsign_norm or None,
                        **sample_weather,
                    )
                    db.fill_pass_identity(
                        recent_pass['id'],
                        manufacturer=identity["manufacturer"],
                        model=identity["model"],
                        registration=identity["registration"],
                        operator=identity["operator"],
                        destination=destination,
                    )
                else:
                    db.log_event({
                        'ts': state_time_sec,
                        'type': 'pass',
                        'icao24': icao24,
                        'callsign': callsign_norm,
                        'distance_km': dist,
                        'altitude_m': int(alt) if alt else None,
                        'speed_kmh': speed_kmh_val,
                        'heading_deg': int(hdg) if hdg else None,
                        'spl_db': noise_associated,
                        'spl_peak_ts': noise_peak_ts,
                        'spl_max_db': noise_peak_max,
                        'in_zone': in_zone_now,
                        'manufacturer': identity["manufacturer"],
                        'model': identity["model"],
                        'registration': identity["registration"],
                        'operator': identity["operator"],
                        'destination': destination,
                        'image_url': (meta or {}).get('image_url'),
                        'radius_km': radius_km,
                        'alt_max_m': alt_max_m,
                        'latitude': lat,
                        'longitude': lon,
                        **sample_weather,
                    })

                # Maintain closest_approach only when both radius and altitude conditions are met
                if in_radius and in_altitude:
                    existing = db.get_closest_approach(icao24)
                    # Use 3D slant range for closest approach comparison
                    existing_r3d = slant_range_km(existing.get('distance_km'), existing.get('altitude_m')) if existing else None
                    if not existing or existing_r3d is None or r3d is not None and r3d < existing_r3d:
                        # Re-evaluate at the closest point with a wider band to catch the true peak.
                        pick_closest = None
                        if noise_association_allowed(dist, alt):
                            pick_closest = claim_noise_peak(icao24, state_time_sec, slant_now, band_s=60.0)
                        noise_associated_closest = pick_closest[0] if pick_closest else None
                        noise_peak_ts_closest = pick_closest[1] if pick_closest else None
                        noise_peak_max_closest = noise_la_max_at(noise_peak_ts_closest)
                        # This sample has the smallest slant range so far, so it is the closest point.
                        closest_lat = lat
                        closest_lon = lon
                        db.log_event({
                        'ts': state_time_sec,
                        'type': 'closest_approach',
                        'icao24': icao24,
                        'callsign': callsign_norm,
                        'distance_km': dist,
                        'altitude_m': int(alt) if alt else None,
                        'speed_kmh': speed_kmh_val,
                        'heading_deg': int(hdg) if hdg else None,
                        'spl_db': noise_associated_closest,
                        'spl_peak_ts': noise_peak_ts_closest,
                        'spl_max_db': noise_peak_max_closest,
                        'in_zone': in_zone_now,
                        'manufacturer': identity["manufacturer"],
                        'model': identity["model"],
                        'registration': identity["registration"],
                        'operator': identity["operator"],
                        'destination': destination,
                        'image_url': (meta or {}).get('image_url'),
                        'radius_km': radius_km,
                        'alt_max_m': alt_max_m,
                        'latitude': closest_lat,
                        'longitude': closest_lon,
                        **weather_for_aircraft(station_snapshot, state_time_sec, lat, lon),
                        })
                        # Also propagate the best noise to the recent pass, so the table reflects the correct peak
                        try:
                            recent_pass_for_update = db.get_recent_pass_for_aircraft(icao24, within_seconds=900)
                            if recent_pass_for_update and noise_associated_closest is not None:
                                # Do not downgrade an existing higher SPL on the pass record
                                current_spl = recent_pass_for_update.get('spl_db')
                                try:
                                    should_update = current_spl is None or float(noise_associated_closest) > float(current_spl)
                                except (TypeError, ValueError):
                                    should_update = True
                                if should_update:
                                    db.update_pass_metrics(
                                        recent_pass_for_update['id'],
                                        spl_db=noise_associated_closest,
                                        spl_peak_ts=noise_peak_ts_closest,
                                        spl_max_db=noise_peak_max_closest,
                                    )
                        except Exception:
                            pass
        globals()['latest_aircraft_snapshot'] = {'ts': time.time(), 'aircraft': snapshot_aircraft}
        logging.info(f"process_aircraft_states: in_range={inrange_count} of {len(states)} states")
    except Exception as e:
        logging.error(f"Error processing aircraft states: {e}")

def fetch_noise_rivm(feed_id):
    response = get_rivm_noise_data()
    if response and response.status_code == 200:
        data = response.json()
        store_noise_data(data)
        logging.info("Polled %s noise points from RIVM", len(data) if hasattr(data, '__len__') else 0)
    else:
        status = response.status_code if response else 'No response'
        logging.warning("RIVM API returned status %s", status)


def fetch_noise_sensor(feed_id):
    config = get_api_config(feed_id) or {}
    sensor_id = config.get('sensor_id') or '69616'
    response = make_api_request(
        api_name=feed_id,
        endpoint='sensor_data',
        params={'sensor_id': sensor_id, 'limit': 100},
        cache_key="noise_poll_{0}".format(sensor_id)
    )
    if response.status_code != 200:
        logging.warning("Sensor API returned status %s", response.status_code)
        return
    transformed_data = []
    for item in response.json():
        noise_value = None
        la_max = None
        for sensor_value in item.get('sensordatavalues', []):
            kind = sensor_value.get('value_type')
            try:
                if kind == 'noise_LAeq':
                    noise_value = float(sensor_value.get('value', 0))
                elif kind == 'noise_LA_max':
                    la_max = float(sensor_value.get('value'))
            except (TypeError, ValueError):
                continue
        if noise_value is None:
            continue
        timestamp_str = item.get('timestamp', '')
        try:
            dt = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            timestamp = dt.timestamp()
        except (TypeError, ValueError):
            timestamp = time.time()
        transformed_data.append({'timestamp': timestamp, 'value': noise_value, 'la_max': la_max})
    store_noise_data(transformed_data)
    logging.info("Polled %s noise points from %s", len(transformed_data), feed_id)


def poll_noise_data():
    """Poll the active noise feed. Fetchers are registered by name in NOISE_FETCHERS."""
    try:
        feed_id = infer_active(db.get_all_admin_settings())['noise']
        feed = feed_by_id(feed_id)
        fetcher = NOISE_FETCHERS.get(feed['fetcher']) if feed else None
        if not fetcher:
            logging.warning("No noise fetcher registered for %s", feed_id)
            return
        fetcher(feed_id)
    except Exception as exc:
        logging.error("Error polling noise data: %s", exc)


NOISE_FETCHERS.update({
    'rivm': fetch_noise_rivm,
    'sensor_community': fetch_noise_sensor,
})

# InfluxDB functions removed - using Sensor.Community API only

NOISE_HISTORY_MAX = 2000   # 10 s samples: about five and a half hours


def store_noise_data(data):
    """Keep noise samples for peak detection, one entry per timestamp.

    Every poll returns the sensor's last samples again, so the batches overlap.
    Repeats are merged; otherwise the buffer fills with copies and covers only
    a few minutes, and a peak surrounded by copies of itself has no prominence.
    """
    global latest_noise_data, noise_data_history
    try:
        if not isinstance(data, list):
            logging.warning(f"Expected list data, got {type(data)}: {data}")
            return
        latest_noise_data = data
        merged = {}
        for entry in list(noise_data_history) + data:
            if isinstance(entry, dict) and 'timestamp' in entry and 'value' in entry:
                merged[float(entry['timestamp'])] = entry
        noise_data_history = [merged[stamp] for stamp in sorted(merged)][-NOISE_HISTORY_MAX:]
        if data:
            logging.info(f"Stored {len(data)} noise data points (history: {len(noise_data_history)} unique) - sample: {data[0]}")
    except Exception as e:
        logging.error(f"Error storing noise data: {e}")


# The recorder's last poll, served to the public map. No upstream call per visitor.
latest_aircraft_snapshot = {'ts': None, 'aircraft': []}

# Health bookkeeping for /api/health and the alert monitor.
_server_started_at = time.time()
_last_poll_tick = None          # the polling loop ran
_last_aircraft_ok = None        # a position poll came back without the feed failing
_last_aircraft_error = None
ALERTER = notifications.Alerter()
_health_thread = None
TRACK_SAMPLE_KEEP_DAYS = 30
_last_sample_prune = 0.0
_last_backup_day = None
_last_backup = {'ts': None, 'path': None, 'error': None}


def retention_settings():
    """Days to keep samples and events. 0 keeps events forever."""
    def _days(key, default, floor):
        try:
            value = int(float(db.get_admin_setting(key, default) or default))
        except (TypeError, ValueError):
            value = default
        return max(floor, value) if value else value
    return {
        'samples_days': _days('retention_samples_days', TRACK_SAMPLE_KEEP_DAYS, 1),
        'events_days': _days('retention_events_days', 0, 30),
        'backup_keep': _days('backup_keep', 7, 1),
    }


def backup_dir():
    """Where backups go: next to the database, inside its data folder."""
    configured = os.getenv('PN_BACKUP_DIR')
    if configured:
        return configured
    db_dir = os.path.dirname(os.path.abspath(db.db_path))
    return os.path.join(db_dir, 'backups') if os.path.basename(db_dir) == 'data' else os.path.join(db_dir, 'data', 'backups')


def backup_database(keep=None):
    """Copy the live database with SQLite's online backup and keep the last `keep` copies.

    Returns the new file's path. Used by the nightly job and scripts/backup_db.py.
    """
    import sqlite3 as _sqlite
    keep = keep or retention_settings()['backup_keep']
    target_dir = backup_dir()
    os.makedirs(target_dir, exist_ok=True)
    stamp = time.strftime('%Y-%m-%d_%H%M')
    target = os.path.join(target_dir, f'plane_noise-{stamp}.db')
    source = _sqlite.connect(db.db_path)
    try:
        dest = _sqlite.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    copies = sorted(name for name in os.listdir(target_dir) if name.startswith('plane_noise-') and name.endswith('.db'))
    for old in copies[:-keep] if keep else []:
        try:
            os.remove(os.path.join(target_dir, old))
        except OSError:
            pass
    _last_backup.update({'ts': time.time(), 'path': target, 'error': None})
    logging.info("Database backed up to %s (%d kept)", target, min(keep, len(copies)))
    return target


REGISTRY_REFRESH_S = 7 * 86400
_airline_designators = None


def _airline_table():
    """The worldwide designator table, loaded once from the database."""
    global _airline_designators
    if _airline_designators is None:
        try:
            _airline_designators = db.get_airline_designators()
        except Exception:
            _airline_designators = {}
    return _airline_designators


def resolve_operator(callsign):
    return airlines.operator_for_callsign(callsign, _airline_table())


def refresh_aircraft_registry(force=False):
    """Download the aircraft registry when it is missing or a week old, then fill unknown rows."""
    try:
        updated = float(db.get_admin_setting('aircraft_registry_updated', '0') or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if not force and db.registry_size() > 0 and time.time() - updated < REGISTRY_REFRESH_S:
        return None
    global _airline_designators
    rows = aircraft_db.fetch_registry()
    count = db.replace_aircraft_registry(rows)
    db.set_admin_setting('aircraft_registry_updated', str(time.time()))
    db.set_admin_setting('aircraft_registry_bytes', str(aircraft_db.last_fetch.get('bytes') or ''))
    db.set_admin_setting('aircraft_registry_source_modified', aircraft_db.last_fetch.get('source_modified') or '')
    _air_meta_cache.clear()
    designators = 0
    try:
        table = airlines.fetch_designators()
        designators = db.replace_airline_designators(table)
        _airline_designators = table
    except Exception as e:
        logging.warning("Airline designators not refreshed: %s", e)
    filled = db.fill_identity_from_registry(0)
    operators = db.fill_operator_from_callsign(resolve_operator, 0)
    logging.info("Aircraft registry loaded: %d aircraft, %d airlines, %d stored passes filled in, %d operators from callsigns",
                 count, designators, filled, operators)
    return {'aircraft': count, 'airlines': designators, 'filled': filled, 'operators': operators}


def run_housekeeping():
    """Hourly pruning by the retention settings, and one backup a day after 03:00 local."""
    scraper_guard.sweep()
    visitor_stats.flush()
    visitor_stats.retain()
    if geo_lookup.needs_refresh():
        try:
            geo_lookup.refresh()
        except Exception:
            logging.exception("The country table could not be refreshed")
    global _last_sample_prune, _last_backup_day
    now = time.time()
    if now - _last_sample_prune > 3600:
        _last_sample_prune = now
        try:
            refresh_aircraft_registry()
            db.fill_identity_from_registry(now - 86400)
            db.fill_operator_from_callsign(resolve_operator, now - 86400)
        except Exception as e:
            logging.warning("Aircraft registry refresh failed: %s", e)
        keep = retention_settings()
        try:
            db.prune_track_samples(now - keep['samples_days'] * 86400)
            if keep['events_days']:
                removed = db.prune_events(now - keep['events_days'] * 86400)
                if removed:
                    logging.info("Retention: removed %d event rows older than %d days", removed, keep['events_days'])
        except Exception as e:
            logging.warning("Retention pruning failed: %s", e)
    try:
        from datetime import datetime as _dt
        local = _dt.now(pytz.timezone(_place_timezone_name()))
        if local.hour >= 3 and _last_backup_day != local.date():
            _last_backup_day = local.date()
            backup_database()
    except Exception as e:
        _last_backup['error'] = str(e)[:200]
        logging.error("Database backup failed: %s", e)

NOISE_PEAK_TOLERANCE_S = 15.0   # two peak times this close apart are the same peak
TRACK_KEEP_S = 1200.0           # how long an aircraft's positions are remembered for the claim rule

# icao24 -> sorted list of (time, slant km). Fed by the recorder, read by the claim rule.
_tracks = {}


def remember_track_point(icao24, ts, slant_km):
    """Keep one position sample of an aircraft for the claim rule."""
    if not icao24 or ts is None or slant_km is None:
        return
    now = float(ts)
    points = _tracks.setdefault(icao24, [])
    points.append((now, float(slant_km)))
    points.sort()
    cutoff = now - TRACK_KEEP_S
    points[:] = [point for point in points if point[0] >= cutoff]
    for other in [key for key, pts in _tracks.items() if not pts or pts[-1][0] < cutoff]:
        _tracks.pop(other, None)


def slant_at(icao24, t):
    """Slant range of an aircraft at time t: interpolated between two remembered samples,
    else the nearest sample within 90 s, else None."""
    points = _tracks.get(icao24)
    if not points:
        return None
    before = [point for point in points if point[0] <= t]
    after = [point for point in points if point[0] >= t]
    if before and after:
        (t0, s0), (t1, s1) = before[-1], after[0]
        if t1 == t0:
            return s0
        return s0 + (s1 - s0) * (t - t0) / (t1 - t0)
    nearest = min(points, key=lambda point: abs(point[0] - t))
    return nearest[1] if abs(nearest[0] - t) <= 90.0 else None


def closest_track_point(icao24):
    """(time, slant km) of the remembered sample nearest the microphone, or None."""
    points = _tracks.get(icao24)
    if not points:
        return None
    return min(points, key=lambda point: point[1])


def pick_noise_peak(target_time_s, band_s=45.0, exclude_ts=(), window_s=180.0, min_prominence_db=5.0):
    """The loudest prominent peak within band_s of the target, as (value, time).

    Peaks at excluded times are skipped. When nothing had to be excluded the
    fallback is the loudest sample in the band. When a peak was excluded and no
    other peak remains, the answer is None: the sound near this aircraft
    belongs to another one. When the band holds no sample at all the answer is
    also None: the noise feed has not caught up yet.
    """
    series = noise_data_history if noise_data_history else latest_noise_data
    if not series:
        return None
    window = []
    for entry in series:
        if 'timestamp' not in entry or 'value' not in entry:
            continue
        stamp = float(entry['timestamp'])
        if target_time_s - window_s <= stamp <= target_time_s + window_s:
            window.append((stamp, float(entry['value'])))
    if not window:
        return None
    window.sort()
    ts = [point[0] for point in window]
    vals = [point[1] for point in window]

    def excluded(stamp):
        return any(abs(stamp - taken) <= NOISE_PEAK_TOLERANCE_S for taken in exclude_ts)

    def in_band(stamp):
        return abs(stamp - target_time_s) <= band_s

    n = len(vals)
    neigh = max(1, min(5, n // 10))
    peaks = []
    for i in range(1, n - 1):
        if vals[i] >= vals[i - 1] and vals[i] >= vals[i + 1]:
            left_min = min(vals[max(0, i - neigh):i])
            right_min = min(vals[i + 1:min(n, i + 1 + neigh)])
            if vals[i] - max(left_min, right_min) >= min_prominence_db:
                peaks.append((ts[i], vals[i]))

    debug = {
        'target_time_s': target_time_s,
        'band_s': band_s,
        'excluded': list(exclude_ts),
        'candidates': [{'val': value, 'dt_s': stamp - target_time_s} for stamp, value in peaks],
    }
    banded = [(stamp, value) for stamp, value in peaks if in_band(stamp) and not excluded(stamp)]
    if banded:
        stamp, value = max(banded, key=lambda point: (point[1], -abs(point[0] - target_time_s)))
        globals()['last_noise_pick_debug'] = {'mode': 'banded_highest', 'chosen_val': value,
                                              'chosen_time_s': stamp, 'meta': debug}
        return round(value, 1), stamp
    if exclude_ts:
        globals()['last_noise_pick_debug'] = {'mode': 'claimed_by_closer_aircraft', 'meta': debug}
        return None
    band_samples = [(stamp, value) for stamp, value in window if in_band(stamp)]
    if band_samples:
        stamp, value = max(band_samples, key=lambda point: (point[1], -abs(point[0] - target_time_s)))
        globals()['last_noise_pick_debug'] = {'mode': 'band_max_no_local', 'chosen_val': value,
                                              'chosen_time_s': stamp, 'meta': debug}
        return round(value, 1), stamp
    # No sample inside the band yet: the sensor feed runs one to two minutes
    # behind the positions. A sample from outside the band would be a reading
    # from before the aircraft arrived, so wait for the recalc worker instead.
    globals()['last_noise_pick_debug'] = {'mode': 'no_samples_in_band_yet', 'meta': debug}
    return None


def find_peak_noise_around_time(target_time_ms: float,
                                prefer_time_s: float = 35.0,
                                window_s: float = 180.0,
                                min_prominence_db: float = 5.0,
                                peak_band_s: float = 45.0) -> Optional[float]:
    """Peak value near a time in milliseconds. Kept for the debug endpoints; the
    recorder uses claim_noise_peak so a peak goes to one aircraft only."""
    try:
        picked = pick_noise_peak((target_time_ms or 0.0) / 1000.0, band_s=peak_band_s,
                                 window_s=window_s, min_prominence_db=min_prominence_db)
        return picked[0] if picked else None
    except Exception as e:
        logging.error(f"Error finding peak noise: {e}")
        return None


def noise_la_max_at(stamp):
    """LA_max of the stored sample at this time, when the feed delivers one."""
    if stamp is None:
        return None
    for entry in noise_data_history:
        try:
            if abs(float(entry.get('timestamp', 0)) - float(stamp)) < 0.5:
                value = entry.get('la_max')
                return float(value) if value is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _row_slant(row):
    """Slant range of a stored row from its distance and altitude."""
    slant = slant_range_km(row.get('distance_km'), row.get('altitude_m'))
    return slant if slant is not None else row.get('distance_km')


def claim_noise_peak(icao24, target_time_s, slant_now, band_s=45.0):
    """The decibel for this aircraft near this time, as (value, peak time), or None.

    A peak belongs to one aircraft: the one closest to the microphone when the
    peak was recorded. A peak that a closer aircraft already holds is skipped.
    A peak held by a farther aircraft is taken over, and that aircraft's rows
    lose it; the recalc worker gives them the next best peak, if any.
    """
    taken = []
    for _ in range(6):
        picked = pick_noise_peak(target_time_s, band_s=band_s, exclude_ts=taken)
        if not picked:
            return None
        value, peak_ts = picked
        ours = slant_at(icao24, peak_ts)
        if ours is None:
            ours = slant_now
        losers = {}
        blocked = False
        for row in db.rows_holding_peak(peak_ts, NOISE_PEAK_TOLERANCE_S, exclude_icao24=icao24):
            theirs = slant_at(row.get('icao24'), peak_ts)
            if theirs is None:
                theirs = _row_slant(row)
            if ours is None or (theirs is not None and theirs <= ours):
                blocked = True
                break
            losers[row.get('icao24')] = row
        if blocked:
            taken.append(peak_ts)
            continue
        for other in losers:
            db.release_noise_peak(other, peak_ts, NOISE_PEAK_TOLERANCE_S)
        return value, peak_ts
    return None


def sensor_position():
    """The point the recorder works around: the detection zone's centre when one
    was saved (latitude/longitude), else the microphone (sensor_lat/sensor_lon,
    what the settings page writes), else the environment default."""
    for lat_key, lon_key in (('latitude', 'longitude'), ('sensor_lat', 'sensor_lon')):
        try:
            lat = float(db.get_admin_setting(lat_key, '') or '')
            lon = float(db.get_admin_setting(lon_key, '') or '')
        except (TypeError, ValueError):
            continue
        if lat or lon:
            return (lat, lon)
    return (SENSOR_LAT, SENSOR_LON)

PRIVACY_JITTER_DEFAULT_M = 200


def privacy_jitter_m():
    """How far the public position may sit from the real sensor. 0 shows the exact spot."""
    try:
        return max(0, min(int(float(db.get_admin_setting('privacy_jitter_m', PRIVACY_JITTER_DEFAULT_M) or 0)), 5000))
    except (TypeError, ValueError):
        return PRIVACY_JITTER_DEFAULT_M


def public_sensor_position():
    """The position the public sees: one fixed decoy 40 to 100 percent of the
    privacy radius away from the real sensor, derived from the secret key.

    Fixed on purpose. A fresh random point per request would let anyone
    average a few hundred answers back to the real spot. The map then moves
    the marker around this decoy on every zoom, so nothing on screen or in
    the API ever marks the address.
    """
    lat, lon = sensor_position()
    radius = privacy_jitter_m()
    if radius <= 0:
        return lat, lon
    digest = hashlib.sha256(f"{app.secret_key}|{lat:.6f}|{lon:.6f}".encode()).digest()
    u = int.from_bytes(digest[:4], 'big') / 2**32
    v = int.from_bytes(digest[4:8], 'big') / 2**32
    distance_m = radius * (0.4 + 0.6 * u)
    angle = 2 * math.pi * v
    dlat = distance_m * math.cos(angle) / 111_320.0
    dlon = distance_m * math.sin(angle) / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
    return round(lat + dlat, 6), round(lon + dlon, 6)


def haversine_km(lat1, lon1, lat2, lon2):
    """Calculate distance between two points in kilometers"""
    R = 6371  # Earth's radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def point_in_polygon(lat, lon, polygon_points):
    """Check if a point is inside a polygon of [latitude, longitude] vertices."""
    if len(polygon_points) < 3:
        return False

    x, y = float(lon), float(lat)
    n = len(polygon_points)
    inside = False

    p1_lat, p1_lon = polygon_points[0]
    p1x, p1y = float(p1_lon), float(p1_lat)
    for i in range(1, n + 1):
        p2_lat, p2_lon = polygon_points[i % n]
        p2x, p2y = float(p2_lon), float(p2_lat)
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y

    return inside

def calculate_polygon_area_km2(points):
    """Calculate polygon area in square kilometers using spherical geometry"""
    if len(points) < 3:
        return 0.0
    
    # Earth's radius in meters
    R = 6371000
    
    # Convert degrees to radians
    def to_rad(deg):
        return deg * math.pi / 180
    
    area = 0.0
    for i in range(len(points)):
        j = (i + 1) % len(points)
        lat1 = to_rad(points[i][0])
        lon1 = to_rad(points[i][1])
        lat2 = to_rad(points[j][0])
        lon2 = to_rad(points[j][1])
        
        area += (lon2 - lon1) * (2 + math.sin(lat1) + math.sin(lat2))
    
    area = abs(area) * R * R / 2
    return area / 1000000  # Convert m² to km²

def is_aircraft_in_detection_zone(lat, lon, alt=None, alt_max=None):
    """Check if aircraft is within the outer detection zone (8km circle)"""
    try:
        # Always use 8km circular zone for outer detection
        sensor_lat, sensor_lon = sensor_position()
        radius_km = float(db.get_admin_setting('radius_km', '8.0'))
        
        distance = haversine_km(sensor_lat, sensor_lon, lat, lon)
        return distance <= radius_km
        
    except Exception as e:
        logging.error(f"Error checking outer detection zone: {e}")
        # Fall back to circular zone
        sensor_lat, sensor_lon = sensor_position()
        radius_km = float(db.get_admin_setting('radius_km', '8.0'))
        distance = haversine_km(sensor_lat, sensor_lon, lat, lon)
        return distance <= radius_km

def is_aircraft_in_inner_zone(lat, lon, alt=None, alt_max=None):
    """Check if aircraft is within the inner detection zone (polygon or circle) for noise measurement"""
    try:
        inner_zone_type = db.get_admin_setting('inner_zone_type', 'circle')
        
        if inner_zone_type == 'polygon':
            # Check polygon zone for inner detection
            points_json = db.get_admin_setting('detection_zone_points', '[]')
            if points_json:
                polygon_points = json.loads(points_json)
                if len(polygon_points) >= 3:
                    return point_in_polygon(lat, lon, polygon_points)
        
        # Fall back to circular inner zone
        sensor_lat, sensor_lon = sensor_position()
        inner_radius_km = float(db.get_admin_setting('noise_assoc_inner_km', '2.5'))
        
        distance = haversine_km(sensor_lat, sensor_lon, lat, lon)
        return distance <= inner_radius_km
        
    except Exception as e:
        logging.error(f"Error checking inner detection zone: {e}")
        # Fall back to circular inner zone
        sensor_lat, sensor_lon = sensor_position()
        inner_radius_km = float(db.get_admin_setting('noise_assoc_inner_km', '2.5'))
        distance = haversine_km(sensor_lat, sensor_lon, lat, lon)
        return distance <= inner_radius_km

def noise_association_allowed(horizontal_km, altitude_m) -> bool:
    """A measured decibel belongs to this aircraft when the 3D slant range is within the inner radius.

    The drawn polygon does not replace this distance. A small tolerance sits on the edge.
    """
    try:
        inner_km = float(db.get_admin_setting('noise_assoc_inner_km', '2.5'))
    except (TypeError, ValueError):
        inner_km = 2.5
    slant = slant_range_km(horizontal_km, altitude_m)
    reach = slant if slant is not None else horizontal_km
    try:
        return reach is not None and float(reach) <= inner_km + 0.2
    except (TypeError, ValueError):
        return False

def slant_range_km(horizontal_km: Optional[float], altitude_m: Optional[float]) -> Optional[float]:
    """Compute 3D slant range given horizontal distance and altitude.

    Returns None if inputs are insufficient.
    """
    try:
        if horizontal_km is None or altitude_m is None:
            return None
        alt_km = float(altitude_m) / 1000.0
        return math.sqrt(float(horizontal_km) ** 2 + alt_km ** 2)
    except Exception:
        return None

# Global variables for noise data
latest_noise_data = None
noise_data_history = []  # Store historical noise data for correlation
last_noise_pick_debug = {}

# --- Post-correlation background worker to handle delayed noise arrival ---
def _get_noise_lag_seconds_default() -> int:
    try:
        from config import Config
        return int(getattr(Config, 'NOISE_CORRELATION_LAG_S', 30))
    except Exception:
        return 30

def get_noise_lag_seconds() -> int:
    try:
        # Admin setting overrides config
        val = db.get_admin_setting('noise_correlation_lag_s', None)
        if val is not None:
            return int(float(val))
    except Exception:
        pass
    return _get_noise_lag_seconds_default()

def recalc_recent_passes_for_noise(window_minutes: int = 15, limit: int = 200) -> int:
    """Re-match recent passes once late noise arrives or a peak changed hands."""
    try:
        recent = db.get_recent_events(limit=limit, event_type='pass')
        if not recent:
            return 0
        lag_s = get_noise_lag_seconds()
        now_s = time.time()
        updated_count = 0
        for ev in recent:
            try:
                ts = float(ev.get('timestamp') or 0)
                if not ts or ts < now_s - (window_minutes * 60):
                    continue
                if not noise_association_allowed(ev.get('distance_km'), ev.get('altitude_m')):
                    continue
                icao24 = ev.get('icao24')
                point = closest_track_point(icao24)
                target, slant = point if point else (ts + lag_s, _row_slant(ev))
                picked = claim_noise_peak(icao24, target, slant, band_s=60.0)
                if not picked:
                    continue
                value, peak_ts = picked
                current = ev.get('spl_db')
                if current is None or float(value) > float(current):
                    db.update_pass_metrics(ev['id'], spl_db=float(value), spl_peak_ts=peak_ts, spl_max_db=noise_la_max_at(peak_ts))
                    updated_count += 1
            except Exception:
                continue
        return updated_count
    except Exception:
        return 0

_post_corr_thread = None
_post_corr_running = False

def start_post_correlation_worker():
    global _post_corr_thread, _post_corr_running
    if _post_corr_running:
        return
    _post_corr_running = True
    import threading
    def _loop():
        while _post_corr_running:
            try:
                recalc_recent_passes_for_noise(window_minutes=15, limit=200)
            except Exception:
                pass
            try:
                label_recent_routes()
            except Exception as e:
                logging.warning("Route labelling failed: %s", e)
            # Run roughly every 45 seconds to catch late spikes
            time.sleep(45)
    _post_corr_thread = threading.Thread(target=_loop, name='post_correlation_worker', daemon=True)
    _post_corr_thread.start()

# Custom Detection Zone API Endpoints
@app.get("/api/admin/get_detection_zone")
@require_admin_auth
def get_detection_zone():
    """Get current detection zone configuration"""
    try:
        inner_zone_type = db.get_admin_setting('inner_zone_type', 'circle')
        lat, lon = sensor_position()
        if inner_zone_type == 'polygon':
            points_json = db.get_admin_setting('detection_zone_points', '[]')
            points = json.loads(points_json) if points_json else []
            return jsonify({
                'zone_type': 'polygon',
                'points': points,
                'sensor': [lat, lon]
            })
        else:
            # Return circular zone info
            radius_km = float(db.get_admin_setting('radius_km', '8.0'))
            return jsonify({
                'zone_type': 'circle',
                'center': [lat, lon],
                'radius_km': radius_km,
                'sensor': [lat, lon]
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.post("/api/admin/save_detection_zone")
@require_admin_auth
def save_detection_zone():
    """Save custom detection zone configuration"""
    try:
        data = request.get_json()
        zone_type = data.get('zone_type', 'circle')
        
        if zone_type == 'polygon':
            points = data.get('points', [])
            if len(points) < 3:
                return jsonify({'error': 'Polygon must have at least 3 points'}), 400
            
            # Calculate polygon area using spherical geometry
            area_km2 = calculate_polygon_area_km2(points)
            
            # Save polygon as inner zone (replaces 2.5km inner circle)
            db.set_admin_setting('inner_zone_type', 'polygon')
            db.set_admin_setting('detection_zone_points', json.dumps(points))
            db.set_admin_setting('polygon_area_km2', str(area_km2))
            
            logging.info(f"Saved custom detection zone with {len(points)} points, area: {area_km2:.2f} km²")
            flagged = db.recompute_zone_flags(_zone_membership_test())
            return jsonify({'success': True, 'points': len(points), 'reflagged': flagged})
        else:
            return jsonify({'error': 'Invalid zone type'}), 400
            
    except Exception as e:
        logging.error(f"Error saving detection zone: {e}")
        return jsonify({'error': str(e)}), 500

@app.post("/api/admin/reset_detection_zone")
@require_admin_auth
def reset_detection_zone():
    """Reset to circular detection zone"""
    try:
        db.set_admin_setting('inner_zone_type', 'circle')
        db.set_admin_setting('detection_zone_points', '[]')
        
        logging.info("Reset to circular detection zone")
        db.recompute_zone_flags(_zone_membership_test())
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error resetting detection zone: {e}")
        return jsonify({'error': str(e)}), 500

@app.get("/api/detection_zone")
def get_public_detection_zone():
    """Get detection zone for public display (no auth required)"""
    try:
        inner_zone_type = db.get_admin_setting('inner_zone_type', 'circle')
        if inner_zone_type == 'polygon':
            points_json = db.get_admin_setting('detection_zone_points', '[]')
            points = json.loads(points_json) if points_json else []
            area_km2 = float(db.get_admin_setting('polygon_area_km2', '0.0'))
            return jsonify({
                'zone_type': 'polygon',
                'points': points,
                'area_km2': area_km2,
                **_noise_gate_info(),
            })
        else:
            # Return inner zone info for public display
            radius_km = float(db.get_admin_setting('noise_assoc_inner_km', '2.5'))
            return jsonify({
                'zone_type': 'circle',
                'center': list(public_sensor_position()),
                'radius_km': radius_km,
                **_noise_gate_info(),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _noise_gate_info():
    """What the map needs to draw the noise range: sensor, slant limit, height cap."""
    try:
        inner_km = float(db.get_admin_setting('noise_assoc_inner_km', '2.5'))
    except (TypeError, ValueError):
        inner_km = 2.5
    try:
        alt_cap = float(db.get_admin_setting('altitude_max_m', db.get_admin_setting('max_altitude_m', '2000')))
    except (TypeError, ValueError):
        alt_cap = 2000.0
    try:
        detect_km = float(db.get_admin_setting('radius_km', '8.0'))
    except (TypeError, ValueError):
        detect_km = 8.0
    lat, lon = public_sensor_position()
    return {
        'sensor': [lat, lon],
        'privacy_jitter_m': privacy_jitter_m(),
        'noise_slant_km': round(inner_km + 0.2, 2),
        'altitude_max_m': alt_cap,
        'detection_radius_km': detect_km,
    }


PHOTO_USER_AGENT = 'plane-noise-dashboard/1.0 (+https://aircraftnoise.eu)'
PHOTO_CACHE_HIT_S = 7 * 86400     # a found photo is kept a week
PHOTO_CACHE_MISS_S = 86400        # a miss is retried after a day
PHOTO_LOOKUPS_PER_MINUTE = 60
_photo_cache = {}                 # 'hex:485a83' / 'reg:PH-BXA' -> (expires_at, payload)
_photo_calls = []                 # timestamps of upstream calls in the last minute


def _photo_from_planespotters(kind, key):
    """One upstream lookup. Returns the photo fields, {} for no photo, None when the call failed."""
    url = f"https://api.planespotters.net/pub/photos/{kind}/{key}"
    try:
        r = requests.get(url, headers={'User-Agent': PHOTO_USER_AGENT, 'Accept': 'application/json'}, timeout=8)
    except Exception as e:
        logging.warning("planespotters lookup failed: %s", e)
        return None
    if r.status_code == 404:
        return {}
    if r.status_code != 200:
        logging.warning("planespotters returned %s for %s", r.status_code, url)
        return None
    try:
        photos = (r.json() or {}).get('photos') or []
    except ValueError:
        return None
    first = photos[0] if photos else None
    thumb = (first or {}).get('thumbnail_large') or (first or {}).get('thumbnail') or {}
    if not first or not thumb.get('src'):
        return {}
    return {'src': thumb['src'], 'link': first.get('link'), 'photographer': first.get('photographer')}


def lookup_aircraft_photo(kind, key):
    """Cached photo lookup. The cache keeps planespotters traffic to a few calls a minute."""
    cache_key = f"{kind}:{key}"
    now = time.time()
    hit = _photo_cache.get(cache_key)
    if hit and hit[0] > now:
        return hit[1]
    _photo_calls[:] = [t for t in _photo_calls if t > now - 60]
    if len(_photo_calls) >= PHOTO_LOOKUPS_PER_MINUTE:
        return hit[1] if hit else None
    _photo_calls.append(now)
    found = _photo_from_planespotters(kind, key)
    if found is None:
        return hit[1] if hit else None
    _photo_cache[cache_key] = (now + (PHOTO_CACHE_HIT_S if found else PHOTO_CACHE_MISS_S), found)
    return found


@app.get("/api/aircraft_photo")
def api_aircraft_photo():
    """Photo for the table's image column: by registration first, then by hex."""
    reg = re.sub(r'[^A-Z0-9-]', '', (request.args.get('reg') or '').upper())
    hexcode = re.sub(r'[^0-9a-f]', '', (request.args.get('hex') or '').lower())
    photo = None
    if 4 <= len(reg) <= 10 and re.search(r'[A-Z]', reg):
        photo = lookup_aircraft_photo('reg', reg)
    if not photo and len(hexcode) == 6:
        photo = lookup_aircraft_photo('hex', hexcode)
    if not photo:
        return jsonify({})
    resp = jsonify(photo)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


def health_snapshot():
    """Facts the alert monitor and /api/health work from. No secrets."""
    now = time.time()
    try:
        newest_noise = max((float(e['timestamp']) for e in noise_data_history if 'timestamp' in e), default=None)
    except (TypeError, ValueError):
        newest_noise = None
    try:
        settings = db.get_all_admin_settings()
        active = infer_active(settings)
        aircraft_interval, noise_interval = _active_poll_intervals()
    except Exception:
        active = {'aircraft': None, 'noise': None}
        aircraft_interval, noise_interval = DEFAULT_AIRCRAFT_POLL_INTERVAL, DEFAULT_NOISE_POLL_INTERVAL
    db_ok, db_error, last_pass_age = True, None, None
    try:
        last = db.get_recent_events(1, 'pass')
        if last:
            last_pass_age = now - float(last[0].get('timestamp') or now)
    except Exception as e:
        db_ok, db_error = False, str(e)[:200]
    usage = _api_usage_stats.get('opensky') or {}
    snapshot = {
        'ok': db_ok,
        'ts': now,
        'uptime_s': now - _server_started_at,
        'noise_feed': active.get('noise'),
        'noise_age_s': (now - newest_noise) if newest_noise else None,
        'noise_samples': len(noise_data_history),
        'aircraft_feed': active.get('aircraft'),
        'aircraft_interval_s': aircraft_interval,
        'aircraft_ok_age_s': (now - _last_aircraft_ok) if _last_aircraft_ok else None,
        'aircraft_last_error': _last_aircraft_error,
        'loop_age_s': (now - _last_poll_tick) if _last_poll_tick else None,
        'last_pass_age_s': last_pass_age,
        'db_ok': db_ok,
        'db_error': db_error,
        'opensky_daily_requests': usage.get('daily_requests'),
        'opensky_daily_limit': usage.get('daily_limit'),
        'last_backup_ts': _last_backup.get('ts'),
        'last_backup_error': _last_backup.get('error'),
        'alerts': {key: dict(value) for key, value in ALERTER.state.items()},
    }
    return snapshot


def run_health_check():
    """One pass of the monitor: evaluate the snapshot and send what is due."""
    settings = notifications.read_settings(db.get_admin_setting)
    snapshot = health_snapshot()
    findings = notifications.evaluate(snapshot, settings)
    ALERTER.process(findings, settings)
    return snapshot, findings


def start_health_monitor(interval_s=60):
    global _health_thread
    if _health_thread is not None:
        return
    def _loop():
        while True:
            try:
                run_health_check()
            except Exception as e:
                logging.warning(f"health monitor: {e}")
            time.sleep(interval_s)
    _health_thread = threading.Thread(target=_loop, name='health_monitor', daemon=True)
    _health_thread.start()


@app.get("/api/health")
def api_health():
    """Public health facts for the outside watchdog and the admin page."""
    snapshot = health_snapshot()
    snapshot['failing'] = [f.key for f in notifications.evaluate(snapshot, notifications.read_settings(db.get_admin_setting)) if f.failing]
    snapshot['ok'] = snapshot['db_ok'] and 'recorder' not in snapshot['failing']
    return jsonify(snapshot)


@app.post("/api/admin/notifications/test")
@require_admin_auth
def api_notifications_test():
    """Send a test message with the saved settings, even while notifications are off."""
    settings = notifications.read_settings(db.get_admin_setting)
    body = request.get_json(silent=True) or {}
    if body.get("method") in ("email", "ntfy"):  # the page's per-method test buttons
        settings = dict(settings, notify_method=body["method"])
        if body["method"] == "email" and not settings.get("notify_email"):
            settings["notify_email"] = session.get("admin_email") or ""  # the mail server test goes to whoever is testing
    delivered, detail = notifications.deliver(
        settings, "Test notification",
        "If you can read this, Aircraft Noise can reach you.", priority="default", tags=["white_check_mark"], force=True)
    return jsonify({'ok': delivered, 'detail': detail}), (200 if delivered else 502)


# ---- routes: arrivals, departures and overflights ---------------------------
def label_pass_route(row):
    """Classify one pass from its stored samples (or its single stored fix) and save the label."""
    ends = db.get_runway_ends()
    airports = db.get_enabled_airports()
    if not ends and not airports:
        return None
    ts = float(row.get('timestamp') or 0)
    samples = db.get_track_samples([row['icao24']], ts - 900, ts + 900).get(row['icao24'], [])
    if not samples and row.get('latitude') is not None:
        samples = [{'ts': ts, 'latitude': row['latitude'], 'longitude': row['longitude'],
                    'altitude_m': row.get('altitude_m'), 'heading_deg': row.get('heading_deg')}]
    if not samples:
        return None
    result = routes.classify_track(samples, ends, airports)
    if row.get('route_confidence') is not None and result['confidence'] <= float(row['route_confidence']):
        return result
    db.set_pass_route(row['id'], result)
    return result


def label_recent_routes(window_s=1800, limit=100):
    """Label passes of the last half hour that have no route or a weak one. Run by the worker."""
    now = time.time()
    done = 0
    for row in db.get_passes_needing_route(now - window_s, now + 60, limit=limit):
        try:
            if label_pass_route(row):
                done += 1
        except Exception as e:
            logging.warning("Route label failed for pass %s: %s", row.get('id'), e)
    return done


@app.post("/api/admin/airports/discover")
@require_admin_auth
def api_airports_discover():
    """Download OurAirports and keep the airports within the radius of the sensor."""
    body = request.get_json(silent=True) or {}
    try:
        radius = max(5.0, min(float(body.get('radius_km') or 60), 200.0))
    except (TypeError, ValueError):
        radius = 60.0
    lat, lon = sensor_position()
    try:
        found = routes.fetch_ourairports(lat, lon, radius)
    except Exception as e:
        return jsonify({'error': 'download_failed', 'details': str(e)[:200]}), 502
    db.replace_airports(found)
    db.set_admin_setting('airports_radius_km', str(radius))
    return jsonify({'airports': db.get_airports(), 'radius_km': radius})


@app.get("/api/admin/airports")
@require_admin_auth
def api_airports_list():
    return jsonify({'airports': db.get_airports(), 'radius_km': db.get_admin_setting('airports_radius_km', '60'),
                    'complaint_suggestion': routes.complaint_suggestion([a['icao'] for a in db.get_enabled_airports()])})


@app.post("/api/admin/airports/<icao>")
@require_admin_auth
def api_airport_toggle(icao):
    body = request.get_json(silent=True) or {}
    ok = db.set_airport_enabled(icao, bool(body.get('enabled', True)))
    return jsonify({'ok': ok, 'airports': db.get_airports()})


@app.post("/api/admin/zone/recompute")
@require_admin_auth
def api_zone_recompute():
    """Set the in-zone flag on every stored pass from the current zone."""
    return jsonify({'ok': True, 'rows': db.recompute_zone_flags(_zone_membership_test())})


@app.post("/api/admin/aircraft_registry/refresh")
@require_admin_auth
def api_registry_refresh():
    """Download the aircraft registry now and fill in every unknown pass."""
    try:
        result = refresh_aircraft_registry(force=True) or {}
        result.update({'ok': True, 'size': db.registry_size()})
        return jsonify(result)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]}), 502


@app.get("/api/admin/aircraft_registry")
@require_admin_auth
def api_registry_status():
    try:
        updated = float(db.get_admin_setting('aircraft_registry_updated', '0') or 0)
    except (TypeError, ValueError):
        updated = 0.0
    try:
        size_bytes = int(float(db.get_admin_setting('aircraft_registry_bytes', '') or 0)) or None
    except (TypeError, ValueError):
        size_bytes = None
    return jsonify({'size': db.registry_size(), 'airlines': len(_airline_table()), 'updated_ts': updated or None,
                    'next_refresh_ts': (updated + REGISTRY_REFRESH_S) if updated else None,
                    'refresh_days': REGISTRY_REFRESH_S // 86400, 'bytes': size_bytes,
                    'source_modified': db.get_admin_setting('aircraft_registry_source_modified', '') or None,
                    'attribution': f"{aircraft_db.ATTRIBUTION} {airlines.ATTRIBUTION}"})


@app.post("/api/admin/routes/relabel")
@require_admin_auth
def api_routes_relabel():
    """Label passes of the last N days again, from their samples or their stored fix."""
    body = request.get_json(silent=True) or {}
    try:
        days = max(1, min(int(body.get('days') or 30), 365))
    except (TypeError, ValueError):
        days = 30
    now = time.time()
    rows = db.get_passes_needing_route(now - days * 86400, now + 60, max_confidence=1.01, limit=20000)
    done = 0
    for row in rows:
        try:
            if label_pass_route(row):
                done += 1
        except Exception:
            continue
    return jsonify({'ok': True, 'labelled': done, 'passes': len(rows)})


@app.get("/api/routes/daily")
def api_routes_daily():
    """Passes per route for today and for the last N days, with the loud bands."""
    try:
        days = max(1, min(request.args.get('days', 14, type=int) or 14, 92))
        zone = pytz.timezone(_place_timezone_name())
        bounds = _local_day_bounds(zone, days)
        today = db.get_route_counts(bounds[-1][1], bounds[-1][2])
        period = db.get_route_counts(bounds[0][1], bounds[-1][2])
        resp = jsonify({'days': days, 'today': today, 'period': period,
                        'airports': [a['icao'] for a in db.get_enabled_airports()]})
        resp.headers['Cache-Control'] = 'public, max-age=60'
        return resp
    except Exception as e:
        return jsonify({'error': 'routes_failed', 'details': str(e)}), 500


@app.get("/api/stats/aircraft_types")
def api_aircraft_types():
    """Aircraft families ranked by their peak corrected to a reference slant distance."""
    try:
        days = max(7, min(request.args.get('days', 365, type=int) or 365, 3650))
        min_n = max(3, min(request.args.get('min_n', aircraft_types.MIN_SAMPLES, type=int) or aircraft_types.MIN_SAMPLES, 200))
        rows = db.get_measured_passes(time.time() - days * 86400)
        prepared = []
        for row in rows:
            slant_km = row.get('peak_slant_km')
            if slant_km is None:
                slant_km = slant_range_km(row.get('distance_km'), row.get('altitude_m'))
            if slant_km is None:
                continue
            prepared.append({'manufacturer': row.get('manufacturer'), 'model': row.get('typecode') or row.get('model'), 'operator': row.get('operator'),
                             'route_kind': row.get('route_kind'), 'spl_db': row.get('spl_db'), 'slant_m': float(slant_km) * 1000.0})
        result = aircraft_types.type_signature(prepared, min_n=min_n)
        result.update({'days': days, 'absorption_db_per_km': aircraft_types.ABSORPTION_DB_PER_KM,
                       'place': db.get_admin_setting('site_place_name', '') or ''})
        resp = jsonify(result)
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    except Exception as e:
        return jsonify({'error': 'types_failed', 'details': str(e)}), 500


@app.get("/types.html")
def types_page():
    try:
        return send_file('types.html')
    except Exception as e:
        logging.error(f"Failed to serve types.html: {e}")
        return "Not Found", 404


@app.get("/api/complaint_target")
def api_complaint_target():
    """Where reports go: the admin's setting, else the directory suggestion for the enabled airports."""
    name = (db.get_admin_setting('complaint_name', '') or '').strip()
    url = (db.get_admin_setting('complaint_url', '') or '').strip()
    source = 'setting'
    if not url:
        suggestion = routes.complaint_suggestion([a['icao'] for a in db.get_enabled_airports()])
        if suggestion:
            name, url, source = suggestion['name'], suggestion['url'], 'directory'
    if url and not url.lower().startswith(('https://', 'http://')):
        url = ''
    return jsonify({'name': name, 'url': url, 'source': source if url else None})


@app.post("/api/admin/backup")
@require_admin_auth
def api_admin_backup():
    """Make a backup now."""
    try:
        path = backup_database()
        return jsonify({'ok': True, 'path': path, 'size_mb': round(os.path.getsize(path) / 1048576, 2)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500


@app.get("/api/admin/poll_schedule")
@require_admin_auth
def api_poll_schedule():
    """The OpenSky quiet-hours schedule and what it costs per day."""
    schedule = opensky_schedule()
    schedule['interval_now_s'] = opensky_interval_now()
    return jsonify(schedule)


@app.get("/api/live_aircraft")
def api_live_aircraft():
    """The recorder's last poll. Served from memory, so visitors cost no upstream calls."""
    snap = latest_aircraft_snapshot or {}
    return jsonify({
        'ts': snap.get('ts'),
        'aircraft': snap.get('aircraft') or [],
        **_noise_gate_info(),
    })


@app.get("/api/tracks")
def api_tracks():
    """Position samples of the given aircraft in a time window, for the flight paths on the map."""
    try:
        codes = [c.strip().lower() for c in (request.args.get('icao24') or '').split(',') if c.strip()]
        if not codes:
            return jsonify({'tracks': {}})
        now = time.time()
        start = float(request.args.get('from') or (now - 86400))
        end = float(request.args.get('to') or now)
        if end - start > 7 * 86400:
            start = end - 7 * 86400
        tracks = db.get_track_samples(codes[:100], start, end)
        return jsonify({'tracks': tracks})
    except (TypeError, ValueError) as e:
        return jsonify({'error': 'bad_request', 'details': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'tracks_failed', 'details': str(e)}), 500

# InfluxDB integration removed - using Sensor.Community API only

# --- Production hardening toggles (env-controlled) ---
try:
    if os.getenv("FLASK_SESSION_SECURE", "false").lower() == "true":
        app.config["SESSION_COOKIE_SECURE"] = True
except Exception:
    pass

# Ensure background polling starts when running under a WSGI server (e.g., Gunicorn)
_polling_started = False

@app.before_request
def _ensure_background_polling():
    start_background_services()


def start_background_services():
    """Start the recorder threads once per process: feed polling, the community
    sync, the noise re-matching worker and the health monitor.

    Called at startup by the dev server's main block and by Gunicorn's
    post_worker_init hook, and as a fallback on the first request. Gunicorn
    must run a single worker: each worker is its own process with its own
    pollers, noise buffer and live snapshot.
    """
    global _polling_started
    if _polling_started:
        return
    try:
        # Load API configs and API usage stats from database
        load_api_configs_from_database()
        load_api_usage_from_database()
        t = threading.Thread(target=background_polling, daemon=True)
        t.start()
        community.start_sync_thread()
        _polling_started = True
        logging.info("Background polling started")
    except Exception as e:
        logging.error(f"Failed to start background polling: {e}")
    try:
        start_post_correlation_worker()
    except Exception as e:
        logging.error(f"Post-correlation worker did not start: {e}")
    try:
        start_health_monitor()
    except Exception as e:
        logging.error(f"Health monitor did not start: {e}")

@app.get("/debug/poll-now")
@require_admin_auth
def debug_poll_now():
    try:
        poll_aircraft_data()
        return jsonify({"status": "ok", "message": "poll_aircraft_data executed"})
    except Exception as e:
        return jsonify({"status": "error", "details": str(e)}), 500

if __name__ == "__main__":
    # Load API configurations and usage stats from database
    load_api_configs_from_database()
    load_api_usage_from_database()
    
    # Initialize TLS configuration (only if not disabled)
    if os.getenv('TLS_DISABLED', 'false').lower() == 'true':
        logging.info("TLS disabled by environment variable")
        tls_config = None
        ssl_context = None
    else:
        tls_config = TLSConfig()
        
        # Setup certificates
        if not tls_config.setup_certificates():
            logging.warning("Failed to setup TLS certificates, running without HTTPS")
            ssl_context = None
        else:
            ssl_context = tls_config.get_ssl_context()
            if ssl_context:
                logging.info("TLS enabled with SSL context")
            else:
                logging.warning("TLS certificates found but SSL context creation failed")
    
    # Add HTTPS redirect if TLS is enabled
    if ssl_context and os.getenv('FORCE_HTTPS', 'true').lower() == 'true':
        redirect_to_https(app)
        
        # Start HTTP redirect server on port 8080
        import threading
        from werkzeug.serving import run_simple
        
        def run_http_redirect():
            def redirect_app(environ, start_response):
                # Redirect all HTTP requests to HTTPS
                https_url = f"https://{environ.get('HTTP_HOST', 'localhost:8787')}{environ.get('PATH_INFO', '')}"
                if environ.get('QUERY_STRING'):
                    https_url += f"?{environ['QUERY_STRING']}"
                
                start_response('301 Moved Permanently', [
                    ('Location', https_url),
                    ('Content-Type', 'text/html'),
                ])
                return [b'<html><body><h1>Redirecting to HTTPS...</h1></body></html>']
            
            print(f"HTTP redirect server started on port 8080 -> https://localhost:8787")
            run_simple('0.0.0.0', 8080, redirect_app, use_reloader=False, threaded=True)
        
        # Start HTTP redirect server in background
        http_thread = threading.Thread(target=run_http_redirect, daemon=True)
        http_thread.start()
    
    # Feed polling, community sync, noise re-matching and the health monitor.
    start_background_services()
    
    # Get server configuration from database
    try:
        external_access = db.get_admin_setting('external_access', 'false').lower() == 'true'
        server_port = int(db.get_admin_setting('server_port', '8787'))
        host = "0.0.0.0" if external_access else "127.0.0.1"
        
        protocol = "HTTPS" if ssl_context else "HTTP"
        print(f"Starting server on {protocol}://{host}:{server_port}")
        print(f"External access: {'Enabled' if external_access else 'Disabled'}")
        if ssl_context:
            print(f"TLS enabled with certificates: {tls_config.cert_file}")
        
        # Start Flask app with or without SSL
        app.run(host=host, port=server_port, debug=False, ssl_context=ssl_context)
    except Exception as e:
        logging.error(f"Error reading server configuration: {e}")
        print("Using default configuration (127.0.0.1:8787)")
        protocol = "HTTPS" if ssl_context else "HTTP"
        print(f"Starting server on {protocol}://127.0.0.1:8787")
        app.run(host="127.0.0.1", port=8787, debug=False, ssl_context=ssl_context)

@app.after_request
def after_request(response):
    # Add CORS headers for remote access
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
    
    # Add security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    
    # Add HTTPS-specific headers if TLS is enabled
    if request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains; preload'
        response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    
    # Allow JavaScript execution for visitor tracking (permissive CSP)
    response.headers['Content-Security-Policy'] = (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "style-src 'self' 'unsafe-inline' data:; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https: http:; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    
    return response

if __name__ == '__main__':
    # Start the server
    import ssl
    from tls_config import get_tls_context
    
    # Get TLS context
    tls_context = get_tls_context()
    
    if tls_context:
        print("Starting server on HTTPS://0.0.0.0:8787")
        print("External access: Enabled")
        print(f"TLS enabled with certificates: tls_certs/server.crt")
        app.run(host='0.0.0.0', port=8787, ssl_context=tls_context, debug=False)
    else:
        print("Starting server on HTTP://0.0.0.0:8787")
        app.run(host='0.0.0.0', port=8787, debug=False)
