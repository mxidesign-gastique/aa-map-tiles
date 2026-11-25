"""Tests for routes_bvv with the value lists of the BVV data model (pp. 57-59); the first pipeline
run checks the real values (build_official.py stops on more than 1 % unknown trail types)."""

import unittest

from routes_bvv import map_bvv, route_id_of

LINE = {"type": "LineString", "coordinates": [[10.7, 47.56], [10.71, 47.57]]}


def trail(**props):
    base = {"FW_NAME": "Lechweg", "FW_TYP": "Wanderweg", "FW_BYNET": "Kein Bayernnetz für Radler", "FW_ID": "4711",
            "FOM_fzwOberflmaterial_BY": "Kein Bitumen, Asphalt, Beton", "FVA_fzwVerkaufkommen_BY": "Abgesetzt, kein oder geringes Verkehrsaufkommen",
            "FLA_fzwLage_BY": "In Bayern", "FRO_fzwRoute_BY": "Hauptroute"}
    base.update(props)
    return {"type": "Feature", "geometry": LINE, "properties": base}


class BvvTest(unittest.TestCase):
    def test_hiking_trail_follows_the_route_schema(self):
        route = map_bvv(trail())
        p = route["properties"]
        self.assertEqual((p["type"], p["network"], p["rank"], p["name"], p["ref"]), ("hiking", "regional", 3, "Lechweg", ""))
        self.assertEqual((p["route_id"], p["source"], p["source_id"]), (4711, "bvv-freizeitwege", "4711"))
        self.assertEqual((p["surface"], p["traffic"]), ("unpaved", "low"))
        self.assertEqual(route["tippecanoe"], {"minzoom": 9})

    def test_local_and_mtb_trails_come_late(self):
        self.assertEqual(map_bvv(trail(FW_TYP="Örtlicher Wanderweg"))["properties"]["rank"], 4)
        mtb = map_bvv(trail(FW_TYP="Moutainbike"))
        self.assertEqual((mtb["properties"]["type"], mtb["tippecanoe"]["minzoom"]), ("mtb", 11))

    def test_bayernnetz_is_the_state_network(self):
        p = map_bvv(trail(FW_TYP="Radweg", FW_BYNET="Bayernnetz für Radler", FOM_fzwOberflmaterial_BY="Bitumen, Asphalt, Beton"))["properties"]
        self.assertEqual((p["type"], p["network"], p["rank"], p["surface"], p["bayernnetz"]), ("cycling", "national", 2, "paved", True))

    def test_extra_route_ranks_lower_and_outside_is_marked(self):
        p = map_bvv(trail(FRO_fzwRoute_BY="Zusatzroute", FLA_fzwLage_BY="Außerhalb Bayerns"))["properties"]
        self.assertEqual((p["rank"], p["outside_bavaria"]), (4, True))

    def test_unknown_surface_and_type(self):
        self.assertEqual(map_bvv(trail(FOM_fzwOberflmaterial_BY="Noch nicht bekannt"))["properties"]["surface"], "unknown")
        self.assertIsNone(map_bvv(trail(FW_TYP="Reitweg")))

    def test_route_id_is_an_integer(self):
        self.assertEqual(route_id_of("12345"), 12345)
        self.assertIsInstance(route_id_of("DEBY-x"), int)


if __name__ == "__main__":
    unittest.main()
