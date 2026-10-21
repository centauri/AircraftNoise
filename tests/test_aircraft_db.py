import os
import tempfile
import time
import unittest

import aircraft_db
import proxy_server
from database import PlaneNoiseDB

SAMPLE = """000001;;;10;;;Miscode - VARIOUS;
484445;PH-AOF;A332;00;AIRBUS A-330-200;;;
3C65C8;D-AINH;A20N;00;AIRBUS A-320neo;;;
A86E6C;N642UA;B763;00;BOEING 767-300;1991;UNITED AIRLINES INC;
501C5C;9A-CAX;BCS3;00;AIRBUS A220-300;;;
ZZZZZZ;bad;;;;;;
"""


class ParseTests(unittest.TestCase):
    def test_rows_and_type_split(self):
        rows = {r["icao24"]: r for r in aircraft_db.parse_rows(SAMPLE)}
        self.assertEqual(set(rows), {"484445", "3c65c8", "a86e6c", "501c5c"})
        self.assertEqual((rows["3c65c8"]["manufacturer"], rows["3c65c8"]["model"], rows["3c65c8"]["typecode"]), ("Airbus", "A-320neo", "A20N"))
        self.assertEqual((rows["a86e6c"]["operator"], rows["a86e6c"]["built"], rows["a86e6c"]["registration"]), ("United Airlines Inc", "1991", "N642UA"))
        self.assertEqual(aircraft_db.split_type_name("EMBRAER ERJ-190-100"), ("Embraer", "ERJ-190-100"))
        self.assertEqual(aircraft_db.split_type_name(""), (None, None))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self._orig = (proxy_server.db, proxy_server._polling_started, aircraft_db.fetch_registry, dict(proxy_server._air_meta_cache))
        proxy_server.db = self.db
        proxy_server._polling_started = True
        proxy_server._air_meta_cache.clear()
        aircraft_db.fetch_registry = lambda timeout=180: aircraft_db.parse_rows(SAMPLE)
        import airlines
        self._orig_fetch_designators = airlines.fetch_designators
        airlines.fetch_designators = lambda timeout=120: {"QQQ": "Quiet Air"}
        proxy_server._airline_designators = None

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started, aircraft_db.fetch_registry, cache = self._orig
        import airlines
        airlines.fetch_designators = self._orig_fetch_designators
        proxy_server._airline_designators = None
        proxy_server._air_meta_cache.clear(); proxy_server._air_meta_cache.update(cache)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_import_lookup_backfill_and_meta(self):
        now = time.time()
        self.db.log_event({"ts": now, "type": "pass", "icao24": "484445", "callsign": "KLM627", "distance_km": 2.0})
        self.db.log_event({"ts": now - 30, "type": "pass", "icao24": "999999", "callsign": "NOWHERE", "distance_km": 2.0})
        self.db.log_event({"ts": now - 60, "type": "pass", "icao24": "888888", "callsign": "QQQ12", "distance_km": 2.0})
        result = proxy_server.refresh_aircraft_registry(force=True)
        self.assertEqual((result["aircraft"], result["airlines"], result["filled"]), (4, 1, 1))
        self.assertEqual(next(r for r in self.db.get_recent_events(5, "pass") if r["icao24"] == "888888")["operator"], "Quiet Air")
        row = next(r for r in self.db.get_recent_events(5, "pass") if r["icao24"] == "484445")
        self.assertEqual((row["manufacturer"], row["model"], row["registration"]), ("Airbus", "A-330-200", "PH-AOF"))
        self.assertIsNone(next(r for r in self.db.get_recent_events(5, "pass") if r["icao24"] == "999999")["model"])
        meta = proxy_server._get_aircraft_meta("A86E6C")
        self.assertEqual((meta["manufacturerName"], meta["typecode"], meta["source"]), ("Boeing", "B763", "registry"))
        # Not a second download within the week
        self.assertIsNone(proxy_server.refresh_aircraft_registry())
        client = proxy_server.app.test_client()
        client.post("/setup/account", json={"name": "A", "email": "a@example.com", "password": "correct horse battery", "confirm": "correct horse battery"})
        self.assertEqual(client.get("/api/admin/aircraft_registry").get_json()["size"], 4)
        self.assertEqual(client.post("/api/admin/aircraft_registry/refresh").get_json()["ok"], True)

    def test_type_ranking_uses_the_registry_type_code(self):
        proxy_server.refresh_aircraft_registry(force=True)
        now = time.time()
        for i in range(5):   # ten minutes apart: each lands in its own five-minute bucket
            self.db.log_event({"ts": now - i * 600, "type": "pass", "icao24": "3c65c8", "callsign": "DLH9CX", "distance_km": 1.5,
                               "altitude_m": 1000, "spl_db": 55.0, "model": "A-320neo", "manufacturer": "Airbus"})
        j = proxy_server.app.test_client().get("/api/stats/aircraft_types?days=7&min_n=3").get_json()
        self.assertEqual(j["groups"]["all"][0]["type"], "A320neo")



class OperatorTests(unittest.TestCase):
    def test_designator_lookup(self):
        import airlines
        self.assertEqual(airlines.operator_for_callsign("KLM35B"), "KLM")
        self.assertEqual(airlines.operator_for_callsign("klc1799"), "KLM Cityhopper")
        self.assertEqual(airlines.operator_for_callsign("CND724"), "Corendon Dutch Airlines")
        self.assertIsNone(airlines.operator_for_callsign("PH-BXA"))       # a registration, not a designator
        self.assertIsNone(airlines.operator_for_callsign("ZZZ123"))       # unknown designator
        self.assertEqual(airlines.operator_for_callsign("ZZZ123", {"ZZZ": "Zed Air"}), "Zed Air")
        self.assertIsNone(airlines.operator_for_callsign(""))

    def test_wikidata_table_drops_dissolved_and_keeps_the_short_name(self):
        import airlines
        csv_text = ("code,name,dissolved\n"
                    "KLM,KLM Asia,\nKLM,KLM,\nKLM,KLM Interinsulair Bedrijf,1949-12-28T00:00:00Z\n"
                    "CND,Corendon Dutch Airlines,\nXX1,Weird,\nBAD,,\n")
        table = airlines.parse_wikidata_csv(csv_text)
        self.assertEqual(table, {"KLM": "KLM", "CND": "Corendon Dutch Airlines", "XX1": "Weird"})

    def test_backfill_fills_only_empty_operators_of_airline_callsigns(self):
        import airlines
        handle = tempfile.NamedTemporaryFile(delete=False)
        path = handle.name
        handle.close()
        db = PlaneNoiseDB(path)
        try:
            now = time.time()
            db.log_event({"ts": now, "type": "pass", "icao24": "a1", "callsign": "KLM627", "distance_km": 2.0})
            db.log_event({"ts": now - 10, "type": "pass", "icao24": "a2", "callsign": "TRA32A", "distance_km": 2.0, "operator": "Transavia Airlines"})
            db.log_event({"ts": now - 20, "type": "pass", "icao24": "a3", "callsign": "PHBXA", "distance_km": 2.0})
            self.assertEqual(db.fill_operator_from_callsign(lambda cs: airlines.operator_for_callsign(cs)), 1)
            ops = {r["callsign"]: r["operator"] for r in db.get_recent_events(5, "pass")}
            self.assertEqual(ops, {"KLM627": "KLM", "TRA32A": "Transavia Airlines", "PHBXA": None})
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)


class RegistryStatusTests(RegistryTests):
    def test_status_reports_size_dates_and_next_refresh(self):
        aircraft_db.last_fetch.update({"bytes": 8203004, "source_modified": "Mon, 20 Oct 2026 01:00:00 GMT"})
        proxy_server.refresh_aircraft_registry(force=True)
        client = proxy_server.app.test_client()
        client.post("/setup/account", json={"name": "A", "email": "a@example.com", "password": "correct horse battery", "confirm": "correct horse battery"})
        j = client.get("/api/admin/aircraft_registry").get_json()
        self.assertEqual((j["bytes"], j["source_modified"], j["refresh_days"]), (8203004, "Mon, 20 Oct 2026 01:00:00 GMT", 7))
        self.assertAlmostEqual(j["next_refresh_ts"] - j["updated_ts"], 7 * 86400, places=3)

if __name__ == "__main__":
    unittest.main()
