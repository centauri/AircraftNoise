"""Operator from the callsign's three-letter ICAO airline designator.

Worldwide names come from Wikidata (CC0): every airline with an ICAO
designator, dissolved ones left out. A short curated list sits on top for
the exact spellings of airlines seen most here. A callsign counts as an
airline flight when three letters are followed by a digit. Nothing in here
imports the server.
"""

import csv
import io
import re

import requests

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_QUERY = (
    'SELECT ?code ?name ?dissolved WHERE { ?airline wdt:P230 ?code . ?airline rdfs:label ?name . '
    'FILTER(LANG(?name) = "en") OPTIONAL { ?airline wdt:P576 ?dissolved } }'
)
ATTRIBUTION = "Airline names: Wikidata (CC0)."
USER_AGENT = "plane-noise-dashboard/1.0 (+https://aircraftnoise.eu)"

AIRLINES = {
    "KLM": "KLM", "KLC": "KLM Cityhopper", "TRA": "Transavia", "TFL": "TUI fly Netherlands",
    "CND": "Corendon Dutch Airlines", "MPH": "Martinair", "TAY": "ASL Airlines Belgium", "ASL": "Air Serbia", "JAF": "TUI fly Belgium",
    "ICE": "Icelandair", "LIF": "Lifeliner air ambulance",
    "BEL": "Brussels Airlines", "EZY": "easyJet", "EJU": "easyJet Europe", "EZS": "easyJet Switzerland",
    "RYR": "Ryanair", "RUK": "Ryanair UK", "WZZ": "Wizz Air", "WMT": "Wizz Air Malta", "VLG": "Vueling",
    "DLH": "Lufthansa", "CLH": "Lufthansa CityLine", "EWG": "Eurowings", "GWI": "Germanwings", "CFG": "Condor",
    "BAW": "British Airways", "CFE": "BA CityFlyer", "SHT": "British Airways Shuttle", "VIR": "Virgin Atlantic",
    "EXS": "Jet2", "TOM": "TUI Airways", "LOG": "Loganair", "EIN": "Aer Lingus", "EUK": "Aer Lingus UK",
    "AFR": "Air France", "HOP": "Air France Hop", "TVF": "Transavia France", "SWR": "Swiss", "EDW": "Edelweiss Air",
    "AUA": "Austrian Airlines", "SAS": "SAS", "SZS": "SAS Connect", "NOZ": "Norwegian", "NAX": "Norwegian",
    "FIN": "Finnair", "IBE": "Iberia", "IBS": "Iberia Express", "ANE": "Air Nostrum", "TAP": "TAP Air Portugal",
    "PGA": "Portugália", "AZA": "ITA Airways", "ITY": "ITA Airways", "LOT": "LOT Polish Airlines", "CSA": "Czech Airlines",
    "BTI": "airBaltic", "AEE": "Aegean", "CTN": "Croatia Airlines", "THY": "Turkish Airlines", "PGT": "Pegasus",
    "SXS": "SunExpress", "UAE": "Emirates", "QTR": "Qatar Airways", "ETD": "Etihad", "SVA": "Saudia",
    "ELY": "El Al", "MSR": "EgyptAir", "RAM": "Royal Air Maroc", "DAL": "Delta", "UAL": "United", "AAL": "American Airlines",
    "ACA": "Air Canada", "JBU": "JetBlue", "SIA": "Singapore Airlines", "CPA": "Cathay Pacific", "CSN": "China Southern",
    "CES": "China Eastern", "CCA": "Air China", "ANA": "All Nippon Airways", "JAL": "Japan Airlines", "KAL": "Korean Air",
    "AIC": "Air India", "MAS": "Malaysia Airlines", "GIA": "Garuda Indonesia", "EVA": "EVA Air", "QFA": "Qantas",
    "SUR": "Suriname Airways", "KQA": "Kenya Airways", "ETH": "Ethiopian Airlines", "AVA": "Avianca", "LAN": "LATAM",
    "CLX": "Cargolux", "GEC": "Lufthansa Cargo", "BCS": "European Air Transport (DHL)", "DHK": "DHL Air UK",
    "FDX": "FedEx", "UPS": "UPS", "ABW": "AirBridgeCargo", "SQC": "Singapore Airlines Cargo", "MSX": "Martinair Cargo",
    "AHO": "Air Hamburg", "NJE": "NetJets", "VJT": "VistaJet", "GAF": "German Air Force", "NAF": "Royal Netherlands Air Force",
}

_PATTERN = re.compile(r"^([A-Z]{3})\d")


def parse_wikidata_csv(text):
    """code -> name. Dissolved airlines are dropped; of several names per code the shortest wins."""
    best = {}
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("code") or "").strip().upper()
        name = (row.get("name") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{3}", code) or not name or (row.get("dissolved") or "").strip():
            continue
        if code not in best or len(name) < len(best[code]):
            best[code] = name
    return best


def fetch_designators(timeout=120):
    """Download the worldwide designator table from Wikidata."""
    r = requests.get(WIKIDATA_SPARQL, params={"query": WIKIDATA_QUERY},
                     headers={"Accept": "text/csv", "User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return parse_wikidata_csv(r.text)


def designator(callsign):
    """The three-letter designator of an airline callsign, or None."""
    m = _PATTERN.match((callsign or "").strip().upper())
    return m.group(1) if m else None


def operator_for_callsign(callsign, table=None):
    """The airline behind a callsign such as KLM35B: the curated spelling first, then the
    worldwide table, else None."""
    code = designator(callsign)
    if not code:
        return None
    return AIRLINES.get(code) or (table or {}).get(code)
