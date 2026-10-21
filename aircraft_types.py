"""Noise signature per aircraft type.

Type names arrive in many spellings ("Boeing 737NG 8K2/W", "B738", "737-8K2")
and are folded into one family here. Each measured peak is then corrected to a
reference slant distance, so types can be compared as if they had all passed
at the same distance. Nothing in here imports the server.
"""

import math
import re
import statistics

REFERENCE_M = 1000.0        # every peak is expressed as if heard from this slant distance
ABSORPTION_DB_PER_KM = 1.0  # air absorption on top of spherical spreading, A-weighted jet noise
MIN_SAMPLES = 10

ICAO_CODES = {
    "B731": "737-100", "B732": "737-200", "B733": "737-300", "B734": "737-400", "B735": "737-500",
    "B736": "737-600", "B737": "737-700", "B738": "737-800", "B739": "737-900",
    "B37M": "737 MAX 7", "B38M": "737 MAX 8", "B39M": "737 MAX 9", "B3XM": "737 MAX 10",
    "B744": "747-400", "B748": "747-8", "B752": "757-200", "B753": "757-300",
    "B762": "767-200", "B763": "767-300", "B764": "767-400",
    "B772": "777-200", "B77L": "777-200LR", "B773": "777-300", "B77W": "777-300ER",
    "B788": "787-8", "B789": "787-9", "B78X": "787-10",
    "A318": "A318", "A319": "A319", "A19N": "A319neo", "A320": "A320", "A20N": "A320neo",
    "A321": "A321", "A21N": "A321neo", "A332": "A330-200", "A333": "A330-300", "A338": "A330-800neo",
    "A339": "A330-900neo", "A342": "A340-200", "A343": "A340-300", "A345": "A340-500", "A346": "A340-600",
    "A359": "A350-900", "A35K": "A350-1000", "A388": "A380", "A221": "A220-100", "A223": "A220-300",
    "E170": "E170", "E75S": "E175", "E75L": "E175", "E190": "E190", "E195": "E195",
    "E290": "E190-E2", "E295": "E195-E2", "AT72": "ATR 72", "AT75": "ATR 72", "AT76": "ATR 72",
    "AT43": "ATR 42", "AT45": "ATR 42", "AT46": "ATR 42", "DH8D": "Dash 8-400", "DH8C": "Dash 8-300",
    "CRJ9": "CRJ-900", "CRJX": "CRJ-1000", "F100": "Fokker 100", "F70": "Fokker 70",
    "BCS1": "A220-100", "BCS3": "A220-300", "SU95": "Superjet 100", "H135": "H135", "EC35": "H135",
}

NOISE_WORDS = ("AIRBUS", "HELICOPTERS", "INDUSTRIE", "SAS", "S.A.", "S.A", "BOEING", "COMPANY", "EMBRAER", "DREAMLINER",
               "STD", "AIRCRAFT", "CORPORATION", "THE", "LTD.", "LTD")


def _clean(text):
    text = (text or "").upper()
    for word in NOISE_WORDS:
        text = re.sub(r"(?<![A-Z])" + re.escape(word) + r"(?![A-Z])", " ", text)
    text = re.sub(r"\(.*?\)", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -/")


def normalise_type(manufacturer, model):
    """One family name for the many spellings of an aircraft type, or None."""
    raw = (model or "").strip()
    if not raw:
        return None
    code = raw.upper().replace(" ", "")
    if code in ICAO_CODES:
        return ICAO_CODES[code]
    text = _clean(f"{manufacturer or ''} {raw}")
    compact = text.replace(" ", "").replace("-", "")

    m = re.search(r"737(?:NG)?[- ]?MAX[- ]?(\d{1,2})", text)
    if m:
        return f"737 MAX {m.group(1)}"
    m = re.search(r"737(?:NG)?[- ]?(\d)(?:K|\d{2}|/|$|\s)", text)
    if m:
        return f"737-{m.group(1)}00"
    m = re.search(r"7(4|5|6|7)7[- ]?(\d)(\d{2})?\s*(ER|LR|F)?", text)
    if m:
        series, digit, suffix = m.group(1), m.group(2), m.group(4) or ""
        if series == "4" and digit == "8":
            return "747-8"
        return f"7{series}7-{digit}00{suffix}"
    m = re.search(r"787[- ]?(8|9|10)", text)
    if m:
        return f"787-{m.group(1)}"
    m = re.search(r"A(220|318|319|320|321|330|340|350|380)[- ]?(\d{3})?([A-Z]*)", compact)
    if m:
        family, series, suffix = m.group(1), m.group(2) or "", m.group(3) or ""
        if family in ("319", "320", "321"):
            # The neo variants carry an N in the suffix (251N, 252NX, 271NX) or a neo series number.
            neo_series = {"151", "153", "171", "251", "252", "253", "271", "272", "273", "291", "292"}
            neo = suffix.startswith("N") or series in neo_series
            return f"A{family}neo" if neo else f"A{family}"
        if family == "330":
            if series[:1] == "9" or series[:1] == "8":
                return "A330-900neo" if series[:1] == "9" else "A330-800neo"
            return f"A330-{series[:1]}00" if series else "A330"
        if family == "350":
            return "A350-1000" if series.startswith("10") else "A350-900"
        if family == "220":
            return f"A220-{series[:1]}00" if series else "A220"
        if family == "340":
            return f"A340-{series[:1]}00" if series else "A340"
        return f"A{family}"
    m = re.search(r"(?:E|EMB|ERJ)[- ]?(170|175|190|195)(?:[- ]?(E2))?", compact)
    if m:
        return f"E{m.group(1)}-E2" if m.group(2) else f"E{m.group(1)}"
    m = re.search(r"ATR[- ]?(42|72)", compact)
    if m:
        return f"ATR {m.group(1)}"
    m = re.search(r"DHC[- ]?8[- ]?(\d)", compact)
    if m:
        return f"Dash 8-{m.group(1)}00"
    return text[:24] if text else None


def normalised_db(spl_db, slant_m, reference_m=REFERENCE_M, absorption=ABSORPTION_DB_PER_KM):
    """The peak as it would be heard at the reference distance.

    Spherical spreading (20 log10 of the distance ratio) plus air absorption per km.
    """
    if spl_db is None or not slant_m or slant_m <= 0:
        return None
    spreading = 20.0 * math.log10(slant_m / reference_m)
    absorption_db = absorption * (slant_m - reference_m) / 1000.0
    return float(spl_db) + spreading + absorption_db


def _quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def type_signature(rows, min_n=MIN_SAMPLES, reference_m=REFERENCE_M):
    """Rank aircraft families by their distance-corrected peak.

    rows: dicts with manufacturer, model, spl_db, slant_m, route_kind, operator.
    Returns {"groups": {"all": [...], "arrival": [...], "departure": [...]}, "min_n", "reference_m",
             "passes": total measured passes used}.
    """
    buckets = {"all": {}, "arrival": {}, "departure": {}}
    used = 0
    for row in rows:
        family = normalise_type(row.get("manufacturer"), row.get("model"))
        value = normalised_db(row.get("spl_db"), row.get("slant_m"), reference_m)
        if not family or value is None:
            continue
        used += 1
        entry = {"norm": value, "raw": float(row["spl_db"]), "slant": float(row["slant_m"]),
                 "operator": (row.get("operator") or "").strip()}
        buckets["all"].setdefault(family, []).append(entry)
        kind = row.get("route_kind")
        if kind in ("arrival", "departure"):
            buckets[kind].setdefault(family, []).append(entry)
    out = {}
    for kind, families in buckets.items():
        ranked = []
        for family, entries in families.items():
            if len(entries) < min_n:
                continue
            norms = [e["norm"] for e in entries]
            operators = {}
            for e in entries:
                if e["operator"]:
                    operators[e["operator"]] = operators.get(e["operator"], 0) + 1
            ranked.append({
                "type": family,
                "n": len(entries),
                "median_db": round(statistics.median(norms), 1),
                "p25_db": round(_quantile(norms, 0.25), 1),
                "p75_db": round(_quantile(norms, 0.75), 1),
                "raw_median_db": round(statistics.median(e["raw"] for e in entries), 1),
                "slant_median_m": int(statistics.median(e["slant"] for e in entries)),
                "operators": [name for name, _ in sorted(operators.items(), key=lambda kv: -kv[1])[:3]],
            })
        ranked.sort(key=lambda r: -r["median_db"])
        out[kind] = ranked
    return {"groups": out, "min_n": min_n, "reference_m": reference_m, "passes": used}
