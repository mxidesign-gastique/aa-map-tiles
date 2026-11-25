"""Tests for hazard_zones against real samples of the services (fixtures/, cut to a few features)."""

import json
import pathlib
import unittest

from hazard_zones import RANK, map_bz, map_lfu, map_wlv

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())["features"]


class WlvTest(unittest.TestCase):
    def test_red_and_yellow_avalanche_zones_are_plan_zones(self):
        zones = [map_wlv(f, "i000901:wlv_gzp_sz_la") for f in load("wlv_gzp_sz_la.json")]
        levels = {z["properties"]["kind"]: z["properties"]["level"] for z in zones}
        self.assertEqual(levels["Summenzone - Rote Zone Lawine"], "high")
        self.assertEqual(levels["Summenzone - Gelbe Zone Lawine"], "medium")
        first = zones[0]["properties"]
        self.assertEqual(first["hazard"], "avalanche")
        self.assertEqual(first["zone_type"], "plan")
        self.assertEqual(first["binding"], "")  # no proof of legal force in the source
        self.assertEqual(first["approved"], "2019-03-07")
        self.assertEqual(first["source_date"], "2024-02-15")
        self.assertEqual(first["municipality"], "Umhausen")
        self.assertTrue(first["id"].startswith("wlv:wlv_gzp_sz_la."))
        self.assertEqual((first["name"], first["link"], first["rule_text"]), ("", "", ""))

    def test_torrent_zone_is_water(self):
        zone = map_wlv(load("wlv_gzp_sz_wb.json")[0], "i000901:wlv_gzp_sz_wb")["properties"]
        self.assertEqual((zone["hazard"], zone["process"], zone["level"]), ("water", "torrent", "high"))

    def test_brown_hint_areas_name_their_process_and_are_never_red(self):
        zones = [map_wlv(f, "i000901:wlv_gzp_bhb")["properties"] for f in load("wlv_gzp_bhb.json")]
        self.assertTrue(all(z["level"] == "hint" and z["zone_type"] == "hint" for z in zones))
        by_label = {z["kind"]: (z["hazard"], z.get("process")) for z in zones}
        self.assertEqual(by_label["Hinweisbereich Braun - Steinschlag"], ("mass_movement", "rockfall"))
        self.assertEqual(by_label["Hinweisbereich Braun - Ohne Angabe"], ("unspecified", None))

    def test_blue_reserved_area(self):
        zone = map_wlv(load("wlv_gzp_bvb.json")[0], "i000901:wlv_gzp_bvb")["properties"]
        self.assertEqual((zone["level"], zone["zone_type"], zone["rank"]), ("reserved", "reserved_area", RANK["reserved"]))

    def test_empty_values_are_dropped(self):
        zone = map_wlv(load("wlv_gzp_bvb.json")[0], "i000901:wlv_gzp_bvb")["properties"]
        self.assertNotIn("process", zone)


class BzTest(unittest.TestCase):
    def test_not_dangerous_is_dropped(self):
        rows = load("bz_avalanches.json")
        kept = [map_bz(f) for f in rows if "nicht gefährlich" in f["properties"]["GEFAHR"]]
        self.assertTrue(kept)
        self.assertTrue(all(z is None for z in kept))

    def test_levels_and_processes(self):
        zones = [z["properties"] for z in map(map_bz, load("bz_avalanches.json") + load("bz_hydrology.json") + load("bz_landslides.json")) if z]
        pairs = {(z["label"], z["level"]) for z in zones}
        self.assertTrue({("H4", "high"), ("H3", "high"), ("H2", "medium")} <= pairs)
        self.assertEqual({z["hazard"] for z in zones}, {"avalanche", "water", "mass_movement"})
        self.assertIn("debris_flow", {z.get("process") for z in zones})
        self.assertTrue(all(z["zone_type"] == "plan" and z["status"] == "Genehmigt" and z["source"] == "bozen" for z in zones))
        self.assertTrue(all(z["id"].startswith("bozen:") for z in zones))


class LfuTest(unittest.TestCase):
    def test_hint_records(self):
        rock = map_lfu({"geometry": None, "properties": {"PROZESS": "Steinschlag/Blockschlag (mit Wald) (Gefahrenhinweisbereich)", "STAND": "01.06.2023"}}, "ghk_sturz_m_wald_epsg4258", 7)["properties"]
        self.assertEqual((rock["hazard"], rock["process"], rock["level"], rock["zone_type"]), ("mass_movement", "rockfall", "hint", "hint"))
        self.assertEqual((rock["source_date"], rock["id"], rock["source"]), ("2023-06-01", "lfu-bayern:ghk_sturz_m_wald:7", "lfu-bayern"))
        deep = map_lfu({"properties": {"PROZESS": "tiefreichende Rutschung (Gefahrenhinweis)", "STAND": "2011"}}, "ghk_tief_rutsch_epsg4258")["properties"]
        self.assertEqual((deep["process"], deep["source_date"]), ("deep_landslide", "2011"))
        self.assertEqual(map_lfu({"properties": {"PROZESS": "Subrosion (Gefahrenhinweis)"}}, "x")["properties"]["process"], "sinkhole")

    def test_overview_is_coverage_not_zone(self):
        self.assertIsNone(map_lfu({"properties": {}}, "ghk_uebersicht_epsg4258"))


if __name__ == "__main__":
    unittest.main()
