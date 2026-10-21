import math
import os
import tempfile
import time
import unittest

import proxy_server
import routes
from database import PlaneNoiseDB

AIRPORTS_CSV = '''"id","ident","type","name","latitude_deg","longitude_deg","elevation_ft","continent","iso_country","iso_region","municipality","scheduled_service","icao_code","iata_code","gps_code","local_code","home_link","wikipedia_link","keywords"
2513,"EHAM","large_airport","Amsterdam Airport Schiphol",52.308601,4.76389,-11,"EU","NL","NL-NH","Amsterdam","yes","EHAM","AMS","EHAM",,,,
9999,"XXFAR","large_airport","Far Away",40.0,-3.0,0,"EU","ES","ES-M","Madrid","yes","XXFA","XXX","XXFA",,,,
'''
RUNWAYS_CSV = '''"id","airport_ref","airport_ident","length_ft","width_ft","surface","lighted","closed","le_ident","le_latitude_deg","le_longitude_deg","le_elevation_ft","le_heading_degT","le_displaced_threshold_ft","he_ident","he_latitude_deg","he_longitude_deg","he_elevation_ft","he_heading_degT","he_displaced_threshold_ft"
237928,2513,"EHAM",12467,198,"ASP",1,0,"18R",52.362701416015625,4.711929798126221,-13,183,886,"36L",52.3286018371582,4.7088398933410645,-12,3,
237999,2513,"EHAM",5000,100,"ASP",1,1,"01","52.30",4.70,-12,10,,"19",52.31,4.70,-12,190,
'''


def _track(lat0, lon0, heading, alt0, climb_ms, n=5, speed_kmh=400):
    out = []
    for i in range(n):
        d = speed_kmh / 3600 * 30 * i
        lat = lat0 + d / 111.2 * math.cos(math.radians(heading))
        lon = lon0 + d / (111.2 * math.cos(math.radians(lat0))) * math.sin(math.radians(heading))
        out.append({"ts": 1000 + 30 * i, "latitude": lat, "longitude": lon, "altitude_m": alt0 + climb_ms * 30 * i})
    return out


class ParserTests(unittest.TestCase):
    def test_parse_keeps_nearby_open_runways_as_two_ends(self):
        found = routes.parse_ourairports(AIRPORTS_CSV, RUNWAYS_CSV, 52.5154, 4.7087, 60)
        self.assertEqual([a["icao"] for a in found], ["EHAM"])
        idents = sorted(r["ident"] for r in found[0]["runways"])
        self.assertEqual(idents, ["18R", "36L"])          # the closed 01/19 is skipped
        end_18r = next(r for r in found[0]["runways"] if r["ident"] == "18R")
        self.assertEqual((end_18r["heading"], end_18r["opposite"], end_18r["length_m"]), (183.0, "36L", 3800))


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        found = routes.parse_ourairports(AIRPORTS_CSV, RUNWAYS_CSV, 52.5154, 4.7087, 60)
        self.ends = [dict(r, airport="EHAM") for r in found[0]["runways"]]
        self.airports = [{"icao": "EHAM", "lat": 52.308601, "lon": 4.76389}]

    def test_arrival_departure_turn_and_overflight(self):
        arrival = routes.classify_track(_track(52.55, 4.712, 183, 2000, -5), self.ends, self.airports)
        self.assertEqual((arrival["kind"], arrival["runway"], arrival["trend"]), ("arrival", "18R", "descending"))
        self.assertGreater(arrival["confidence"], 0.8)
        departure = routes.classify_track(_track(52.40, 4.709, 3, 900, 8), self.ends, self.airports)
        self.assertEqual((departure["kind"], departure["runway"]), ("departure", "36L"))
        turning = routes.classify_track(_track(52.46, 4.69, 330, 1200, 8), self.ends, self.airports)
        self.assertEqual((turning["kind"], turning["runway"], turning["label"]), ("departure", None, "EHAM departure"))
        over = routes.classify_track(_track(52.50, 4.60, 90, 3000, 0), self.ends, self.airports)
        self.assertEqual((over["kind"], over["label"]), ("overflight", "overflight E"))

    def test_single_fix_uses_its_heading(self):
        one = [{"ts": 1000, "latitude": 52.55, "longitude": 4.712, "altitude_m": 1800, "heading_deg": 183}]
        res = routes.classify_track(one, self.ends, self.airports)
        self.assertEqual((res["kind"], res["runway"]), ("arrival", "18R"))
        self.assertLess(res["confidence"], 0.8)      # no climb or descent known

    def test_descending_the_wrong_way_is_not_an_arrival(self):
        res = routes.classify_track(_track(52.55, 4.712, 3, 2000, -5), self.ends, self.airports)   # heading away, descending
        self.assertNotEqual(res["kind"], "arrival")


class RouteEndpointTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.path = handle.name
        handle.close()
        self.db = PlaneNoiseDB(self.path)
        self.db.set_admin_setting("latitude", "52.5154")
        self.db.set_admin_setting("longitude", "4.7087")
        self._orig = (proxy_server.db, proxy_server._polling_started, routes.fetch_ourairports)
        proxy_server.db = self.db
        proxy_server._polling_started = True
        routes.fetch_ourairports = lambda lat, lon, radius, timeout=60: routes.parse_ourairports(AIRPORTS_CSV, RUNWAYS_CSV, lat, lon, radius)
        self.client = proxy_server.app.test_client()
        self.client.post("/setup/account", json={"name": "A", "email": "a@example.com", "password": "correct horse battery", "confirm": "correct horse battery"})

    def tearDown(self):
        proxy_server.db, proxy_server._polling_started, routes.fetch_ourairports = self._orig
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_discover_toggle_label_and_stats(self):
        j = self.client.post("/api/admin/airports/discover", json={"radius_km": 60}).get_json()
        self.assertEqual([a["icao"] for a in j["airports"]], ["EHAM"])
        self.assertTrue(j["airports"][0]["enabled"])
        self.assertEqual(self.client.get("/api/complaint_target").get_json()["source"], "directory")
        # A pass with samples on the 18R approach gets its label from the worker's routine
        now = time.time()
        self.db.log_event({"ts": now, "type": "pass", "icao24": "abc123", "callsign": "KLM1", "distance_km": 3.0,
                           "altitude_m": 1900, "spl_db": 57.0, "latitude": 52.55, "longitude": 4.712, "heading_deg": 183})
        for s in _track(52.55, 4.712, 183, 2000, -5):
            self.db.add_track_sample(ts=now + s["ts"] - 1000, icao24="abc123", callsign="KLM1", latitude=s["latitude"],
                                     longitude=s["longitude"], altitude_m=int(s["altitude_m"]), distance_km=3.0, slant_km=3.5, heading_deg=183)
        self.assertEqual(proxy_server.label_recent_routes(), 1)
        row = self.db.get_recent_events(1, "pass")[0]
        self.assertEqual((row["route_kind"], row["route_runway"], row["route_label"]), ("arrival", "18R", "EHAM 18R arrival"))
        daily = self.client.get("/api/routes/daily?days=2").get_json()
        self.assertEqual(daily["today"][0]["label"], "EHAM 18R arrival")
        self.assertEqual(daily["today"][0]["ge55"], 1)
        # Disabling the airport removes its runways from the classifier and the suggestion
        self.client.post("/api/admin/airports/EHAM", json={"enabled": False})
        self.assertEqual(self.db.get_runway_ends(), [])
        self.assertIsNone(self.client.get("/api/complaint_target").get_json()["source"])
        # An explicit setting wins over the directory
        self.db.set_admin_setting("complaint_url", "https://example.org/noise")
        self.db.set_admin_setting("complaint_name", "Example office")
        t = self.client.get("/api/complaint_target").get_json()
        self.assertEqual((t["name"], t["source"]), ("Example office", "setting"))
        self.assertIn("route_label", self.client.get("/recent_events?limit=1").get_json()["events"][0])


if __name__ == "__main__":
    unittest.main()
