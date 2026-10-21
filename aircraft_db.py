"""A local aircraft registry: hex code to registration, type and operator.

The data is the Mictronics aircraft database as published by the tar1090-db
project (https://github.com/wiedehopf/tar1090-db), about 8 MB compressed for
some 620,000 aircraft. Downloaded into the site's own database, it answers
every identity lookup locally, with no API call and no daily cap. Nothing in
here imports the server.
"""

import gzip
import io
import re

import requests

TAR1090_DB_URL = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"
USER_AGENT = "plane-noise-dashboard/1.0 (+https://aircraftnoise.eu)"
ATTRIBUTION = "Aircraft identities: Mictronics aircraft database via tar1090-db."

MANUFACTURERS = ("AIRBUS", "BOEING", "EMBRAER", "BOMBARDIER", "ATR", "CESSNA", "PIPER", "BEECH", "DASSAULT",
                 "GULFSTREAM", "FOKKER", "SAAB", "DE HAVILLAND", "DIAMOND", "ROBINSON", "EUROCOPTER", "AGUSTA",
                 "SIKORSKY", "LEARJET", "PILATUS", "CIRRUS", "MCDONNELL DOUGLAS", "LOCKHEED", "ANTONOV", "TUPOLEV",
                 "SOCATA", "TECNAM", "CANADAIR", "MITSUBISHI", "SUKHOI", "COMAC", "BELL", "GROB", "AEROSPATIALE")


def split_type_name(long_name):
    """'AIRBUS A-320neo' -> ('Airbus', 'A-320neo'); unknown makers keep the whole text as model."""
    text = (long_name or "").strip()
    if not text:
        return None, None
    upper = text.upper()
    for maker in sorted(MANUFACTURERS, key=len, reverse=True):
        if upper.startswith(maker + " ") or upper == maker:
            rest = text[len(maker):].strip(" -")
            return maker.title().replace("De Havilland", "De Havilland").replace("Mcdonnell", "McDonnell"), (rest or None)
    return None, text


def parse_rows(text):
    """Rows of the tar1090 CSV: hex;registration;typecode;flags;type name;built;operator;..."""
    out = []
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 3:
            continue
        icao = parts[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{6}", icao):
            continue
        registration = parts[1].strip() or None
        typecode = parts[2].strip().upper() or None
        type_name = parts[4].strip() if len(parts) > 4 else ""
        if type_name.startswith("Miscode"):
            type_name = ""
        built = parts[5].strip() if len(parts) > 5 else ""
        operator = parts[6].strip() if len(parts) > 6 else ""
        if not (registration or typecode or type_name):
            continue
        manufacturer, model = split_type_name(type_name)
        out.append({
            "icao24": icao,
            "registration": registration,
            "typecode": typecode,
            "type_name": type_name or None,
            "manufacturer": manufacturer,
            "model": model or typecode,
            "operator": (operator.title() if operator.isupper() else operator) or None,
            "built": built or None,
        })
    return out


last_fetch = {"bytes": None, "source_modified": None}


def fetch_registry(timeout=180):
    """Download and parse the whole database. Size and publication date land in last_fetch."""
    r = requests.get(TAR1090_DB_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    last_fetch["bytes"] = len(r.content)
    last_fetch["source_modified"] = r.headers.get("Last-Modified")
    text = gzip.GzipFile(fileobj=io.BytesIO(r.content)).read().decode("utf-8", "replace")
    return parse_rows(text)
