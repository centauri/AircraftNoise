import gzip
import os
import tempfile
import unittest

import geo


class GeoTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "geo.db")

    def test_keys_sort_like_addresses(self):
        self.assertLess(geo.ip_key("1.0.0.0"), geo.ip_key("1.0.0.255"))
        self.assertLess(geo.ip_key("1.0.0.255"), geo.ip_key("1.0.1.0"))
        self.assertLess(geo.ip_key("::ffff:203.0.113.9"), geo.ip_key("2001:db8::1"))
        self.assertEqual(len(geo.ip_key("2001:db8::1")), 16)

    def test_refresh_and_lookup(self):
        csv = "0.0.0.0,0.255.255.255,ZZ\n1.0.0.0,1.0.0.255,AU\n1.0.1.0,1.0.3.255,CN\n" + "\n".join(
            "10.{0}.{1}.0,10.{0}.{1}.255,NL".format(i // 256, i % 256) for i in range(1100)) + "\n2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,DE\n"
        payload = gzip.compress(csv.encode())
        asked = []

        def fetch(month):
            asked.append(month)
            return payload if len(asked) == 2 else None   # this month not out yet, last month is

        g = geo.Geo(path=self.path, city_path=os.path.join(self.dir, "none.mmdb"), fetch=fetch)
        self.assertTrue(g.needs_refresh())
        result = g.refresh()
        self.assertEqual(result["rows"], 1103)
        self.assertEqual(len(asked), 2)
        self.assertEqual(g.country("1.0.0.7"), "AU")
        self.assertEqual(g.country("1.0.2.1"), "CN")
        self.assertEqual(g.country("10.2.7.7"), "NL")
        self.assertEqual(g.country("2001:db8::42"), "DE")
        self.assertIsNone(g.country("1.0.4.0"), "a gap between ranges is unknown")
        self.assertIsNone(g.country("0.1.2.3"), "ZZ ranges are left out")
        self.assertIsNone(g.country("not an address"))
        self.assertIsNone(g.city("1.0.0.7"), "no city database, no city")
        st = g.status()
        self.assertEqual((st["rows"], st["month"], st["city_db"]), (1103, asked[1], False))
        self.assertFalse(g.needs_refresh())
        self.assertTrue(geo.Geo(path=self.path, fetch=fetch).country("1.0.0.7") == "AU", "the file is reopened as it is")

    def test_refresh_refuses_an_empty_table(self):
        g = geo.Geo(path=self.path, fetch=lambda month: gzip.compress(b"1.0.0.0,1.0.0.255,AU\n"))
        with self.assertRaises(RuntimeError):
            g.refresh()
        self.assertEqual(g.status()["rows"], 0)


if __name__ == "__main__":
    unittest.main()
