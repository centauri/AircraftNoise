"""Feed catalog.

Add a feed by appending one entry to FEEDS and registering a fetcher
under the entry's ``fetcher`` name in proxy_server.py.
"""

import copy

_NUMBER = "number"
_BOOLEAN = "boolean"

FEEDS = [
    {
        "id": "noise",
        "kind": "noise",
        "fetcher": "sensor_community",
        "name": "Sensor.Community Airrohr",
        "summary": "Citizen noise sensor. No account.",
        "docs": "https://sensor.community/en/",
        "tracking_key": "noise_sensor",
        "log_usage": False,
        "url_hint": "sensor.community",
        "usage": {
            "daily_limit": None,
            "rate_limit_per_minute": 60,
            "pricing": "Free (no official limit)",
            "reset_type": "none",
        },
        "config": {
            "name": "Sensor.Community Airrohr",
            "base_url": "https://data.sensor.community/airrohr/v1/sensor",
            "endpoints": {
                "sensor_data": "/{sensor_id}/?limit={limit}",
                "inspect": "https://maps.sensor.community/airrohr/{sensor_id}",
                "csv": "https://api-rrd.madavi.de/airrohr/csv/{sensor_id}",
            },
            "poll_interval_seconds": 300,
            "rate_limit": 60,
            "cache_timeout": 30,
            "timeout": 30,
            "headers": {
                "User-Agent": "plane-noise-dashboard/1.0",
                "Accept": "application/json",
            },
            "auth_required": False,
            "sensor_id": "69616",
        },
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url", "help": "Base URL for this sensor feed."},
            {"key": "sensor_id", "label": "Sensor ID", "type": "text", "help": "Sensor.Community sensor ID."},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
            {"key": "poll_interval_seconds", "label": "Poll interval (seconds)", "type": _NUMBER, "min": 5, "max": 3600, "step": 5, "help": "How often the server asks this feed."},
        ],
    },
    {
        "id": "noise_rivm",
        "kind": "noise",
        "fetcher": "rivm",
        "name": "RIVM Samen Meten",
        "summary": "Dutch open noise and air measurements. No account.",
        "docs": "https://www.samenmeten.nl/dataportaal/api-application-programming-interface",
        "tracking_key": "noise_rivm",
        "log_usage": False,
        "url_hint": "samenmeten.rivm.nl",
        "usage": {
            "daily_limit": None,
            "rate_limit_per_minute": 120,
            "pricing": "Free (no official limit)",
            "reset_type": "none",
        },
        "config": {
            "name": "RIVM Samen Meten",
            "base_url": "https://api-samenmeten.rivm.nl/v1.0",
            "endpoints": {
                "things": "/Things",
                "sensors": "/Sensors",
                "observations": "/Datastreams({datastream_id})/Observations",
                "locations": "/Locations",
                "filtered_things": "/Things?$filter=startswith(name,'{filter}')",
                "nearby_locations": "/Locations?$filter=geo.distance(location,geography'SRID=4326;POINT({lon} {lat})') le {distance}",
            },
            "poll_interval_seconds": 300,
            "rate_limit": 120,
            "cache_timeout": 300,
            "timeout": 30,
            "headers": {
                "User-Agent": "plane-noise-dashboard/1.0",
                "Accept": "application/json",
            },
            "auth_required": False,
            "thing_id": "",
            "latitude": 0.0,
            "longitude": 0.0,
        },
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url", "help": "RIVM SensorThings base URL."},
            {"key": "thing_id", "label": "Thing ID", "type": "text", "help": "Leave empty to search near the coordinates."},
            {"key": "latitude", "label": "Latitude", "type": _NUMBER, "step": 0.000001},
            {"key": "longitude", "label": "Longitude", "type": _NUMBER, "step": 0.000001},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
            {"key": "poll_interval_seconds", "label": "Poll interval (seconds)", "type": _NUMBER, "min": 5, "max": 3600, "step": 5},
        ],
    },
    {
        "id": "aircraft",
        "kind": "aircraft",
        "fetcher": "opensky",
        "auth": "opensky",
        "name": "OpenSky Network",
        "summary": "Live aircraft positions. Free tier is about 4000 requests a day.",
        "docs": "https://openskynetwork.github.io/opensky-api/",
        "tracking_key": "opensky",
        "log_usage": True,
        "url_hint": "opensky-network.org",
        "multi_setting": "opensky",
        "multi_default": True,
        "multi_priority": 1,
        "usage": {
            "daily_limit": 4000,
            "rate_limit_per_minute": 30,
            "pricing": "Free (4000 requests/day)",
            "reset_type": "daily",
        },
        "config": {
            "name": "OpenSky Network",
            "base_url": "https://opensky-network.org/api",
            "endpoints": {
                "states_all": "/states/all?lamin={lamin}&lomin={lomin}&lamax={lamax}&lomax={lomax}",
                "flight_track": "/tracks/all",
            },
            "poll_interval_seconds": 30,
            "rate_limit": 30,
            "cache_timeout": 120,
            "timeout": 20,
            "headers": {"User-Agent": "plane-noise-dashboard/1.0"},
            "auth_required": True,
        },
        "secrets": [
            {
                "key": "clientId",
                "label": "Client ID",
                "help": "OpenSky OAuth client ID. Leave blank to keep the saved value.",
            },
            {
                "key": "clientSecret",
                "label": "Client secret",
                "help": "OpenSky OAuth client secret. Leave blank to keep the saved value.",
            },
            {
                "key": "username",
                "label": "Username",
                "help": "Optional. Used only when the client ID is empty.",
            },
            {
                "key": "password",
                "label": "Password",
                "help": "Optional. Used only when the client secret is empty.",
            },
        ],
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url"},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
            {"key": "poll_interval_seconds", "label": "Poll interval (seconds)", "type": _NUMBER, "min": 5, "max": 3600, "step": 5},
        ],
    },
    {
        "id": "aircraft_airplanes_live",
        "kind": "aircraft",
        "fetcher": "airplanes_live",
        "name": "Airplanes.live",
        "summary": "Community ADS-B. About one request per second, no daily cap, no uptime promise.",
        "docs": "https://airplanes.live/api-docs/#/paths/v2-point-lat---lon---radius/get",
        "tracking_key": "airplanes_live",
        "log_usage": True,
        "url_hint": "airplanes.live",
        "multi_setting": "airplanes_live",
        "multi_default": False,
        "multi_priority": 3,
        "usage": {
            "daily_limit": None,
            "rate_limit_per_minute": 60,
            "pricing": "Free (1 req/sec, no daily limit)",
            "reset_type": "none",
        },
        "config": {
            "name": "Airplanes.live",
            "base_url": "https://api.airplanes.live/v2",
            "endpoints": {"point": "/point/{lat}/{lon}/{radius}"},
            "poll_interval_seconds": 300,
            "rate_limit": 60,
            "cache_timeout": 30,
            "timeout": 20,
            "headers": {"User-Agent": "plane-noise-dashboard/1.0"},
            "auth_required": False,
        },
        "secrets": [
            {
                "key": "airplanes_live_key",
                "label": "API key",
                "help": "Optional. Sent as the auth header. The public host ignores it; set the base URL to https://rest.api.airplanes.live when you use a key.",
            }
        ],
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url", "help": "The poller calls /point with the sensor location and radius."},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
            {"key": "poll_interval_seconds", "label": "Poll interval (seconds)", "type": _NUMBER, "min": 2, "max": 3600, "step": 1, "help": "Stay at 2 seconds or slower."},
        ],
    },
    {
        "id": "aircraft_adsb_fi",
        "kind": "aircraft",
        "fetcher": "adsb_fi",
        "name": "adsb.fi",
        "summary": "Community ADS-B from Finland with worldwide feeders. One request per second, no daily cap. Personal use, with a credit in the footer.",
        "docs": "https://github.com/adsbfi/opendata",
        "tracking_key": "adsb_fi",
        "log_usage": True,
        "url_hint": "adsb.fi",
        "multi_setting": "adsb_fi",
        "multi_default": False,
        "multi_priority": 4,
        "usage": {
            "daily_limit": None,
            "rate_limit_per_minute": 60,
            "pricing": "Free for personal use (1 req/sec, no daily limit)",
            "reset_type": "none",
        },
        "config": {
            "name": "adsb.fi",
            "base_url": "https://opendata.adsb.fi/api/v3",
            "endpoints": {"point": "/lat/{lat}/lon/{lon}/dist/{radius}"},
            "poll_interval_seconds": 300,
            "rate_limit": 60,
            "cache_timeout": 30,
            "timeout": 20,
            "headers": {"User-Agent": "plane-noise-dashboard/1.0"},
            "auth_required": False,
        },
        "secrets": [],
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url", "help": "The poller asks /lat/{lat}/lon/{lon}/dist/{radius} around the sensor."},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
            {"key": "poll_interval_seconds", "label": "Poll interval (seconds)", "type": _NUMBER, "min": 2, "max": 3600, "step": 1, "help": "Stay at 2 seconds or slower."},
        ],
    },
    {
        "id": "aircraft_adsb_lol",
        "kind": "aircraft",
        "fetcher": "adsb_lol",
        "name": "adsb.lol",
        "summary": "Community ADS-B under ODbL. The public API allows 500 requests a day, so this is a slow fallback: three minutes between polls at best.",
        "docs": "https://www.adsb.lol/docs/open-data/api/",
        "tracking_key": "adsb_lol",
        "log_usage": True,
        "url_hint": "adsb.lol",
        "multi_setting": "adsb_lol",
        "multi_default": False,
        "multi_priority": 5,
        "usage": {
            "daily_limit": 500,
            "rate_limit_per_minute": 60,
            "pricing": "Free (500 requests a day per address)",
            "reset_type": "daily",
        },
        "config": {
            "name": "adsb.lol",
            "base_url": "https://api.adsb.lol/v2",
            "endpoints": {"point": "/point/{lat}/{lon}/{radius}"},
            "poll_interval_seconds": 300,
            "rate_limit": 60,
            "cache_timeout": 60,
            "timeout": 20,
            "headers": {"User-Agent": "plane-noise-dashboard/1.0"},
            "auth_required": False,
        },
        "secrets": [],
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url", "help": "The poller asks /point/{lat}/{lon}/{radius} around the sensor."},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
            {"key": "poll_interval_seconds", "label": "Poll interval (seconds)", "type": _NUMBER, "min": 180, "max": 3600, "step": 10, "help": "500 requests a day: 180 seconds or slower."},
        ],
    },
    {
        "id": "aircraft_flightaware",
        "kind": "aircraft",
        "fetcher": "flightaware",
        "auth": "flightaware",
        "name": "FlightAware AeroAPI",
        "summary": "Looks up type and destination for a pass already seen, inside the free $5 credit. Off switches it to position searches, which cost more.",
        "docs": "https://www.flightaware.com/aeroapi/portal/documentation",
        "tracking_key": "flightaware",
        "log_usage": True,
        "url_hint": "flightaware.com",
        "multi_setting": "flightaware",
        "multi_default": True,
        "multi_priority": 2,
        "usage": {
            "daily_limit": None,
            "rate_limit_per_minute": 10,
            "pricing": "Free tier: $5/month credit",
            "reset_type": "monthly",
        },
        "config": {
            "name": "FlightAware AeroAPI",
            "base_url": "https://aeroapi.flightaware.com/aeroapi",
            "endpoints": {
                "flights_search": "/flights/search",
                "positions": "/flights/search/positions",
                "flight_info": "/flights/{fa_flight_id}",
                "aircraft_info": "/aircraft/{ident}",
                "airport_info": "/airports/{airport_code}",
                "positions": "/flights/{fa_flight_id}/positions",
                "track": "/flights/{fa_flight_id}/track",
            },
            "poll_interval_seconds": 300,
            "rate_limit": 10,
            "cache_timeout": 300,
            "timeout": 15,
            "headers": {
                "User-Agent": "plane-noise-dashboard/1.0",
                "Accept": "application/json",
            },
            "auth_required": True,
            "free_tier": True,
            "free_usage": "$5/month credit",
            "spend_beyond_credit": False,
            "enrichment_only": True,
        },
        "secrets": [
            {
                "key": "flightaware_key",
                "label": "API key",
                "help": "Sent as the x-apikey header. Leave blank to keep the saved value.",
            }
        ],
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url"},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
            {"key": "poll_interval_seconds", "label": "Poll interval (seconds)", "type": _NUMBER, "min": 5, "max": 3600, "step": 5},
            {
                "key": "enrichment_only",
                "label": "Enrichment only",
                "type": _BOOLEAN,
                "help": "On looks up one new pass about every 48 minutes, up to thirty a day, for type, registration, and destination. That spreads the free $5 credit through the day, so a busy hour does not use it all. Off uses position searches instead. Those cost about ten times as much, so they stay at about three a day unless spending past the credit is on.",
            },
            {
                "key": "spend_beyond_credit",
                "label": "Spend past the $5 credit",
                "type": _BOOLEAN,
                "help": "Applies when enrichment only is off. Off stops position searches after about three calls a day. On keeps searching when OpenSky fails, and FlightAware can charge the account.",
            },
        ],
    },
    {
        "id": "weather",
        "kind": "other",
        "fetcher": None,
        "name": "OpenWeatherMap",
        "summary": "Surface wind, rain, and temperature for a recorded pass. Called with the station snapshot, not on its own timer.",
        "docs": "https://openweathermap.org/api",
        "tracking_key": "weather",
        "log_usage": True,
        "url_hint": "openweathermap.org",
        "usage": {
            "daily_limit": None,
            "rate_limit_per_minute": 60,
            "pricing": "Free: 1000 calls/day, Paid: varies",
            "reset_type": "daily",
        },
        "config": {
            "name": "OpenWeatherMap",
            "base_url": "https://api.openweathermap.org/data/2.5",
            "endpoints": {
                "current": "/weather?lat={lat}&lon={lon}&appid={api_key}",
                "forecast": "/forecast?lat={lat}&lon={lon}&appid={api_key}",
            },
            "poll_interval_seconds": 300,
            "rate_limit": 60,
            "cache_timeout": 300,
            "timeout": 10,
            "headers": {"User-Agent": "plane-noise-dashboard/1.0"},
            "auth_required": False,
        },
        "secrets": [
            {
                "key": "openweather_key",
                "label": "API key",
                "help": "OpenWeatherMap key. Leave blank to keep the saved value.",
            }
        ],
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url"},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
        ],
    },
    {
        "id": "open_meteo",
        "kind": "other",
        "fetcher": None,
        "name": "Open-Meteo",
        "summary": "Temperature at 2 m and 80 m for the inversion sentence. No key. Called with the station snapshot, not on its own timer.",
        "docs": "https://open-meteo.com/en/docs",
        "tracking_key": "open_meteo",
        "log_usage": True,
        "url_hint": "api.open-meteo.com",
        "usage": {
            "daily_limit": 10000,
            "rate_limit_per_minute": 60,
            "pricing": "Free non-commercial. Attribution required (CC BY 4.0).",
            "reset_type": "daily",
        },
        "config": {
            "name": "Open-Meteo",
            "base_url": "https://api.open-meteo.com/v1",
            "endpoints": {
                "forecast": "/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m,temperature_80m",
            },
            "rate_limit": 60,
            "cache_timeout": 300,
            "timeout": 10,
            "headers": {"User-Agent": "plane-noise-dashboard/1.0"},
            "auth_required": False,
        },
        "secrets": [],
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url"},
            {"key": "rate_limit", "label": "Rate limit (per minute)", "type": _NUMBER, "min": 1, "max": 1000, "step": 1},
            {"key": "cache_timeout", "label": "Cache timeout (seconds)", "type": _NUMBER, "min": 0, "max": 3600, "step": 10},
            {"key": "timeout", "label": "Request timeout (seconds)", "type": _NUMBER, "min": 5, "max": 120, "step": 5},
        ],
    },
]

_SHARED_NUMERIC = ("rate_limit", "cache_timeout", "timeout", "poll_interval_seconds")


def feed_by_id(feed_id):
    for feed in FEEDS:
        if feed["id"] == feed_id:
            return feed
    return None


def feeds_by_kind(kind):
    return [feed for feed in FEEDS if feed["kind"] == kind]


def tracking_key_for(api_name):
    feed = feed_by_id(api_name)
    if not feed:
        return None
    return feed["tracking_key"]


def usage_template():
    stats = {}
    for feed in FEEDS:
        row = {
            "name": feed["name"],
            "daily_requests": 0,
            "last_reset": None,
            "remaining_requests": None,
        }
        row.update(feed["usage"])
        stats[feed["tracking_key"]] = row
    return stats


def _field(feed, key):
    for field in feed["fields"]:
        if field["key"] == key:
            return field
    return None


def coerce_field(field, raw):
    if field["type"] == _NUMBER:
        number = float(raw)
        step = field.get("step")
        if step is None or float(step).is_integer():
            return int(number)
        return number
    if field["type"] == _BOOLEAN:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return str(raw)


def _url_belongs_elsewhere(feed, value):
    text = str(value)
    for other in FEEDS:
        hint = other.get("url_hint")
        if other["id"] != feed["id"] and hint and hint in text:
            return True
    return False


def _legacy_owner(settings, prefix):
    url = settings.get(prefix + "_base_url", "")
    matches = []
    for feed in FEEDS:
        hint = feed.get("url_hint")
        if hint and hint in url and feed["id"].startswith(prefix):
            matches.append(feed["id"])
    if len(matches) == 1:
        return matches[0]
    if prefix == "noise":
        return "noise_rivm" if "rivm" in url else "noise"
    if "airplanes.live" in url:
        return "aircraft_airplanes_live"
    if "adsb.fi" in url:
        return "aircraft_adsb_fi"
    if "adsb.lol" in url:
        return "aircraft_adsb_lol"
    if "flightaware" in url:
        return "aircraft_flightaware"
    return "aircraft"


def materialize_configs(settings):
    """Catalog defaults, then saved per-feed values, then the last shared provider slot."""
    settings = settings or {}
    owners = {
        "noise": _legacy_owner(settings, "noise"),
        "aircraft": _legacy_owner(settings, "aircraft"),
    }
    configs = {}
    for feed in FEEDS:
        config = copy.deepcopy(feed["config"])
        for field in feed["fields"]:
            raw = settings.get("{0}_{1}".format(feed["id"], field["key"]))
            if raw is None:
                continue
            value = coerce_field(field, raw)
            if field["key"] == "base_url" and _url_belongs_elsewhere(feed, value):
                continue
            config[field["key"]] = value
        prefix = "noise" if feed["kind"] == "noise" else "aircraft" if feed["kind"] == "aircraft" else None
        if prefix and owners.get(prefix) == feed["id"]:
            for key in _SHARED_NUMERIC + ("base_url",):
                own_key = "{0}_{1}".format(feed["id"], key)
                if own_key in settings:
                    continue
                raw = settings.get("{0}_{1}".format(prefix, key))
                if raw is None:
                    continue
                field = _field(feed, key)
                if not field:
                    continue
                value = coerce_field(field, raw)
                if key == "base_url" and _url_belongs_elsewhere(feed, value):
                    continue
                config[key] = value
        configs[feed["id"]] = config
    return configs


def infer_active(settings):
    settings = settings or {}
    noise_ids = {feed["id"] for feed in feeds_by_kind("noise")}
    aircraft_ids = {feed["id"] for feed in feeds_by_kind("aircraft")}
    noise = settings.get("active_noise_feed")
    if noise not in noise_ids:
        noise = _legacy_owner(settings, "noise")
    aircraft = settings.get("active_aircraft_feed")
    if aircraft != "multi" and aircraft not in aircraft_ids:
        if str(settings.get("multi_api_multi_api", "")).lower() == "true":
            aircraft = "multi"
        else:
            aircraft = _legacy_owner(settings, "aircraft")
    return {"noise": noise, "aircraft": aircraft}


def multi_members(settings):
    settings = settings or {}
    rows = []
    for feed in FEEDS:
        key = feed.get("multi_setting")
        if not key:
            continue
        raw_enabled = settings.get("multi_api_enable_{0}".format(key))
        if raw_enabled is None:
            enabled = bool(feed.get("multi_default", False))
        else:
            enabled = str(raw_enabled).lower() == "true"
        raw_priority = settings.get("multi_api_{0}_priority".format(key))
        try:
            priority = int(raw_priority) if raw_priority is not None else int(feed.get("multi_priority", 9))
        except (TypeError, ValueError):
            priority = int(feed.get("multi_priority", 9))
        rows.append({
            "id": feed["id"],
            "name": feed["name"],
            "setting": key,
            "enabled": enabled,
            "priority": priority,
        })
    rows.sort(key=lambda row: row["priority"])
    return rows


def public_field(field, value):
    shown = {
        "key": field["key"],
        "label": field["label"],
        "type": field["type"],
        "value": value if value is not None else "",
    }
    for extra in ("help", "min", "max", "step"):
        if extra in field:
            shown[extra] = field[extra]
    return shown
