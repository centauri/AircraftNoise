"""Runways as data, routes as geometry.

Airports and runway ends come from OurAirports (public domain) for the area
around the sensor. A recorded track is labelled as an arrival on a runway, a
departure from one, or an overflight, from its heading, its climb or descent
and where it sits relative to each runway's extended centreline. Nothing in
here knows any particular airport. Nothing in here imports the server.
"""

import csv
import io
import math

import requests

OURAIRPORTS_AIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OURAIRPORTS_RUNWAYS = "https://davidmegginson.github.io/ourairports-data/runways.csv"
USER_AGENT = "plane-noise-dashboard/1.0 (+https://aircraftnoise.eu)"

AIRPORT_TYPES = ("large_airport", "medium_airport", "small_airport")
HEADING_TOLERANCE = 25.0       # degrees between track and runway heading
MAX_RUNWAY_RANGE_KM = 45.0     # beyond this a runway is not considered
MAX_ARRIVAL_ALT_M = 3500.0
CLIMB_MS = 1.5                 # vertical speed that counts as climbing / descending

# Where noise complaints go, by airport code. Only a suggestion the admin can
# change; an airport that is not listed simply has no suggestion.
COMPLAINT_DIRECTORY = {
    "EHAM": {"name": "BAS (Bewoners Aanspreekpunt Schiphol)", "url": "https://www.bezoekbas.nl/"},
}


# ------------------------------------------------------------------ geometry

def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff(a, b):
    """Smallest difference between two headings, 0..180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def local_xy_km(lat, lon, origin_lat, origin_lon):
    """East and north offsets in km on a flat local plane. Good to tens of km."""
    east = math.radians(lon - origin_lon) * 6371.0 * math.cos(math.radians(origin_lat))
    north = math.radians(lat - origin_lat) * 6371.0
    return east, north


def along_and_cross_km(lat, lon, threshold_lat, threshold_lon, heading):
    """Position relative to a runway end: along the runway heading (negative is
    before the threshold, on the approach side) and across it (left is negative)."""
    east, north = local_xy_km(lat, lon, threshold_lat, threshold_lon)
    h = math.radians(heading)
    ux, uy = math.sin(h), math.cos(h)          # unit vector along the heading, east/north
    along = east * ux + north * uy
    cross = east * uy - north * ux
    return along, cross


# ------------------------------------------------------------------ OurAirports

def parse_ourairports(airports_text, runways_text, lat, lon, radius_km):
    """Airports within radius_km of (lat, lon) with their runway ends.

    Returns a list of {icao, name, type, lat, lon, distance_km, municipality,
    country, runways: [{ident, lat, lon, heading, length_m, opposite}]}.
    """
    found = {}
    for row in csv.DictReader(io.StringIO(airports_text)):
        if row.get("type") not in AIRPORT_TYPES:
            continue
        icao = (row.get("icao_code") or row.get("ident") or "").strip().upper()
        if not icao:
            continue
        try:
            a_lat, a_lon = float(row["latitude_deg"]), float(row["longitude_deg"])
        except (TypeError, ValueError, KeyError):
            continue
        distance = haversine_km(lat, lon, a_lat, a_lon)
        if distance > radius_km:
            continue
        found[row.get("ident", icao).strip().upper()] = {
            "icao": icao, "name": row.get("name", ""), "type": row.get("type", ""),
            "lat": a_lat, "lon": a_lon, "distance_km": round(distance, 1),
            "municipality": row.get("municipality", ""), "country": row.get("iso_country", ""),
            "scheduled": (row.get("scheduled_service") or "").lower() == "yes",
            "runways": [],
        }
    if not found:
        return []
    for row in csv.DictReader(io.StringIO(runways_text)):
        ident = (row.get("airport_ident") or "").strip().upper()
        airport = found.get(ident)
        if airport is None or (row.get("closed") or "0").strip() == "1":
            continue
        try:
            length_m = float(row.get("length_ft") or 0) * 0.3048
        except ValueError:
            length_m = 0.0
        for side, other in (("le", "he"), ("he", "le")):
            try:
                end_lat = float(row[f"{side}_latitude_deg"])
                end_lon = float(row[f"{side}_longitude_deg"])
                heading = float(row[f"{side}_heading_degT"])
            except (TypeError, ValueError, KeyError):
                continue
            ident_end = (row.get(f"{side}_ident") or "").strip().upper()
            if not ident_end:
                continue
            airport["runways"].append({
                "ident": ident_end, "lat": end_lat, "lon": end_lon, "heading": heading,
                "length_m": round(length_m), "opposite": (row.get(f"{other}_ident") or "").strip().upper(),
            })
    airports = [a for a in found.values() if a["runways"]]
    airports.sort(key=lambda a: a["distance_km"])
    return airports


def fetch_ourairports(lat, lon, radius_km, timeout=60):
    """Download both OurAirports files and keep the airports around the sensor."""
    headers = {"User-Agent": USER_AGENT}
    airports = requests.get(OURAIRPORTS_AIRPORTS, headers=headers, timeout=timeout)
    airports.raise_for_status()
    runways = requests.get(OURAIRPORTS_RUNWAYS, headers=headers, timeout=timeout)
    runways.raise_for_status()
    return parse_ourairports(airports.text, runways.text, lat, lon, radius_km)


# ------------------------------------------------------------------ classification

def _track_motion(samples):
    """Heading of travel, vertical speed in m/s and the first and last fixes."""
    fixes = [s for s in samples if s.get("latitude") is not None and s.get("longitude") is not None]
    fixes.sort(key=lambda s: float(s.get("ts") or 0))
    if not fixes:
        return None
    first, last = fixes[0], fixes[-1]
    span_km = haversine_km(first["latitude"], first["longitude"], last["latitude"], last["longitude"])
    if len(fixes) >= 2 and span_km >= 0.5:
        heading = bearing_deg(first["latitude"], first["longitude"], last["latitude"], last["longitude"])
    else:
        known = [float(s["heading_deg"]) for s in fixes if s.get("heading_deg") is not None]
        heading = known[-1] if known else None
    vertical = None
    alts = [(float(s["ts"]), float(s["altitude_m"])) for s in fixes if s.get("altitude_m") is not None and s.get("ts")]
    if len(alts) >= 2 and alts[-1][0] > alts[0][0]:
        vertical = (alts[-1][1] - alts[0][1]) / (alts[-1][0] - alts[0][0])
    return {"heading": heading, "vertical_ms": vertical, "first": first, "last": last, "fixes": len(fixes),
            "altitude_m": alts[-1][1] if alts else None}


def _trend(vertical_ms):
    if vertical_ms is None:
        return "unknown"
    if vertical_ms >= CLIMB_MS:
        return "climbing"
    if vertical_ms <= -CLIMB_MS:
        return "descending"
    return "level"


def _sector(heading):
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int(((heading + 22.5) % 360) // 45)]


def classify_track(samples, runway_ends, airports=None):
    """Label a track from its samples and the known runway ends.

    runway_ends: [{airport, ident, lat, lon, heading, length_m}]
    airports: [{icao, lat, lon}] for the runway-less fallback.
    Returns {kind, airport, runway, label, confidence, heading, trend}.
    """
    motion = _track_motion(samples)
    if motion is None:
        return {"kind": "unknown", "airport": None, "runway": None, "label": None, "confidence": 0.0,
                "heading": None, "trend": "unknown"}
    heading = motion["heading"]
    trend = _trend(motion["vertical_ms"])
    pos = motion["last"]
    best = None
    if heading is not None:
        for end in runway_ends:
            err = angle_diff(heading, float(end["heading"]))
            if err > HEADING_TOLERANCE:
                continue
            along, cross = along_and_cross_km(pos["latitude"], pos["longitude"], end["lat"], end["lon"], float(end["heading"]))
            if abs(along) > MAX_RUNWAY_RANGE_KM:
                continue
            cone = 1.0 + 0.12 * abs(along)          # the extended centreline widens with distance
            if abs(cross) > cone:
                continue
            if along < 0:
                kind = "arrival"                    # still before the threshold, flying toward it
                plausible = trend in ("descending", "level", "unknown") and (motion["altitude_m"] or 0) <= MAX_ARRIVAL_ALT_M
            else:
                kind = "departure"                  # past the threshold, flying away along the runway heading
                plausible = trend in ("climbing", "level", "unknown")
            if not plausible:
                continue
            fit = (1 - err / HEADING_TOLERANCE) * 0.6 + (1 - abs(cross) / cone) * 0.4
            if trend == "level":
                fit *= 0.75
            candidate = {"kind": kind, "airport": end["airport"], "runway": end["ident"], "fit": fit,
                         "distance_km": abs(along)}
            if best is None or candidate["fit"] > best["fit"]:
                best = candidate
    if best is not None:
        label = f"{best['airport']} {best['runway']} {best['kind']}"
        confidence = min(0.98, 0.5 + best["fit"] / 2)
        if trend == "unknown":
            # One fix and no climb or descent: a weak label, revisited once samples exist.
            confidence = min(confidence, 0.55)
        return {"kind": best["kind"], "airport": best["airport"], "runway": best["runway"], "label": label,
                "confidence": round(confidence, 2), "heading": heading, "trend": trend}

    # No runway aligned: an aircraft climbing away from, or descending toward, an airport is still an
    # arrival or departure there, with the runway unknown (a turn after take-off, for example).
    if airports and motion["fixes"] >= 2:
        first, last = motion["first"], motion["last"]
        for airport in airports:
            d_first = haversine_km(first["latitude"], first["longitude"], airport["lat"], airport["lon"])
            d_last = haversine_km(last["latitude"], last["longitude"], airport["lat"], airport["lon"])
            if max(d_first, d_last) > MAX_RUNWAY_RANGE_KM:
                continue
            if trend == "climbing" and d_last > d_first + 0.3:
                return {"kind": "departure", "airport": airport["icao"], "runway": None,
                        "label": f"{airport['icao']} departure", "confidence": 0.4, "heading": heading, "trend": trend}
            if trend == "descending" and d_last < d_first - 0.3 and (motion["altitude_m"] or 0) <= MAX_ARRIVAL_ALT_M:
                return {"kind": "arrival", "airport": airport["icao"], "runway": None,
                        "label": f"{airport['icao']} arrival", "confidence": 0.4, "heading": heading, "trend": trend}

    label = f"overflight {_sector(heading)}" if heading is not None else "overflight"
    return {"kind": "overflight", "airport": None, "runway": None, "label": label,
            "confidence": 0.3 if heading is not None else 0.1, "heading": heading, "trend": trend}


def complaint_suggestion(airport_icaos):
    """The first listed airport that has a known complaint office."""
    for icao in airport_icaos:
        entry = COMPLAINT_DIRECTORY.get(str(icao).upper())
        if entry:
            return dict(entry, airport=str(icao).upper())
    return None
