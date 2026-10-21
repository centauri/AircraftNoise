import os
import tempfile
import time
import unittest

import aircraft_types
import proxy_server
from database import PlaneNoiseDB


class NormaliseTests(unittest.TestCase):
    def test_spellings_fold_into_families(self):
        cases = {
            ("Boeing", "737NG 8K2/W"): "737-800", ("Airbus Industrie", "737-8K2"): "737-800", (None, "B738"): "737-800",
            (None, "Boeing 737 MAX 9"): "737 MAX 9", ("Boeing", "777 306ER"): "777-300ER", (None, "Boeing 787-10 Dreamliner"): "787-10",
            ("Airbus Sas", "AIRBUS A320-214"): "A320", (None, "Airbus A320-251N"): "A320neo", (None, "A21N"): "A321neo",
            ("AIRBUS SAS", "A321-271NX"): "A321neo", (None, "Airbus A330-941"): "A330-900neo", ("Airbus", "A330 303"): "A330-300",
            ("Embraer", "EMB-190 STD"): "E190", (None, "Embraer E195-E2"): "E195-E2", (None, "E75L"): "E175",
            ("Embraer", "EMB-175 200STD"): "E175", (None, "A350-1041"): "A350-1000", (None, ""): None,
        }
        for key, want in cases.items():
            self.assertEqual(aircraft_types.normalise_type(*key), want, key)

    def test_distance_correction(self):
        # Twice the distance: +6 dB spreading, +1 dB absorption for the extra km.
        self.assertAlmostEqual(aircraft_types.normalised_db(50.0, 2000.0), 57.0, places=1)
        self.assertAlmostEqual(aircraft_types.normalised_db(50.0, 1000.0), 50.0, places=3)
        self.assertIsNone(aircraft_types.normalised_db(None, 1000.0))
        self.assertIsNone(aircraft_types.normalised_db(50.0, 0))


class SignatureTests(unittest.TestCase):
    def test_ranking_uses_medians_and_the_minimum(self):
        rows = []
        for i in range(12):
            rows.append({"manufacturer": "Boeing", "model": "737NG 8K2/W", "spl_db": 58 + (i % 3), "slant_m": 1500, "route_kind": "departure", "operator": "KLM"})
        for i in range(12):
            rows.append({"manufacturer": None, "model": "A20N", "spl_db": 52 + (i % 3), "slant_m": 1500, "route_kind": "arrival", "operator": "Transavia"})
        rows.append({"manufacturer": None, "model": "E195-E2", "spl_db": 70, "slant_m": 1000, "route_kind": "arrival", "operator": "KLC"})
        sig = aircraft_types.type_signature(rows, min_n=10)
        all_types = [r["type"] for r in sig["groups"]["all"]]
        self.assertEqual(all_types, ["737-800", "A320neo"])            # the lone E195-E2 is below the minimum
        self.assertEqual([r["type"] for r in sig["groups"]["departure"]], ["737-800"])
        top = sig["groups"]["all"][0]
        self.assertEqual(top["n"], 12)
        self.assertEqual(top["operators"], ["KLM"])
        self.assertGreater(top["median_db"], top["raw_median_db"])     # 1,500 m corrected up to 1,000 m
        self.assertEqual(sig["passes"], 25)


class TypesEndpointTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self._orig = (proxy_server.db, proxy_server._polling_started)
        proxy_server.db = self.db
        proxy_server._polling_started = True

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started = self._orig
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_endpoint_prefers_the_sample_at_the_peak(self):
        now = time.time()
        for i in range(6):
            ts = now - i * 600
            self.db.log_event({"ts": ts, "type": "pass", "icao24": f"ab{i:04d}", "callsign": "KLM1", "model": "B738",
                               "distance_km": 3.0, "altitude_m": 2000, "spl_db": 55.0, "spl_peak_ts": ts + 30})
            # A sample at the peak time much closer than the stored closest point
            self.db.add_track_sample(ts=ts + 30, icao24=f"ab{i:04d}", callsign="KLM1", latitude=52.5, longitude=4.7,
                                     altitude_m=900, distance_km=0.5, slant_km=1.03, heading_deg=180)
        client = proxy_server.app.test_client()
        j = client.get("/api/stats/aircraft_types?days=30&min_n=5").get_json()
        row = j["groups"]["all"][0]
        self.assertEqual(row["type"], "737-800")
        self.assertEqual(row["slant_median_m"], 1030)
        self.assertAlmostEqual(row["median_db"], 55.3, places=1)
        self.assertEqual(client.get("/types.html").status_code, 200)


if __name__ == "__main__":
    unittest.main()
