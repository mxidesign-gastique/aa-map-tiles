"""Tests of protected_areas.py with small inline fixtures derived from the TypeScript tests."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import protected_areas as pa  # noqa: E402

SHARED_KEYS = [
    "id", "name", "kind", "source", "source_date", "binding", "link", "rule_text",
    "zone_class", "period_from", "period_to", "period_text",
]

SQUARE = {"type": "Polygon", "coordinates": [[[10, 46], [11, 46], [11, 47], [10, 47], [10, 46]]]}
LINE = {"type": "LineString", "coordinates": [[10, 46], [11, 47]]}


def feature(properties, geometry=SQUARE, fid=None):
    result = {"type": "Feature", "properties": properties, "geometry": geometry}
    if fid is not None:
        result["id"] = fid
    return result


class Base(unittest.TestCase):
    def check_shape(self, zone):
        self.assertEqual(list(zone["properties"].keys()), SHARED_KEYS)
        self.assertEqual(zone["type"], "Feature")


class ProtectionTime(unittest.TestCase):
    def test_periods_over_the_turn_of_the_year(self):
        cases = {
            "20.12. - 30.04.": ("12-20", "04-30"),
            "01.12. – 30.04.": ("12-01", "04-30"),
            "16.12 - 31.03.": ("12-16", "03-31"),
            "10.01.- 31.07..": ("01-10", "07-31"),
            "01.11. - 30.06.": ("11-01", "06-30"),
        }
        for text, period in cases.items():
            self.assertEqual(pa.parse_protection_time(text), ("period", period), text)

    def test_all_year_unreadable_and_none(self):
        self.assertEqual(pa.parse_protection_time("01.01. - 31.12."), ("all_year", None))
        self.assertEqual(pa.parse_protection_time("01.01.-31.12."), ("all_year", None))
        for text in ["En cas de neige.", "16.12. bis Ende Skisaison.", "Skisaison.", "01.12. bis Ostern.", "31.02. - 31.03.",
                     "24 dicembre - 31 marzo / 15 aprile - 15 agosto."]:
            self.assertEqual(pa.parse_protection_time(text), ("unreadable", None), text)
        self.assertEqual(pa.parse_protection_time("  "), ("none", None))
        self.assertEqual(pa.parse_protection_time(None), ("none", None))

    def test_without_number(self):
        self.assertEqual(pa.without_number("Weisshorn-Urdental (Nr. 210.0)"), "Weisshorn-Urdental")
        self.assertEqual(pa.without_number("Piz Ela (Nr. 20.00)"), "Piz Ela")
        self.assertEqual(pa.without_number("Ohne Nummer"), "Ohne Nummer")


class Bafu(Base):
    def park(self, kategorie, name, fid):
        return feature({"name": name, "kategorie": kategorie, "rechtsgrundlage": "1", "status": "P"}, fid=fid)

    def test_national_park_is_rules_and_other_parks_are_outlines(self):
        snp = self.park("SNP", "Schweizerischer Nationalpark", 37)
        rn = self.park("RN", "Naturpark Thal", 20)
        ne = self.park("NE", "Parc naturel périurbain du Jorat", 36)
        zone = pa.map_bafu_np(snp)
        self.check_shape(zone)
        self.assertEqual(zone["geometry"], SQUARE)
        self.assertEqual(zone["properties"], {
            "id": "bafu-np:37", "name": "Schweizerischer Nationalpark", "kind": "Nationalpark", "source": "bafu-schutzgebiete",
            "source_date": "", "binding": "", "link": "", "rule_text": "", "zone_class": "rules",
            "period_from": "", "period_to": "", "period_text": "",
        })
        self.assertIsNone(pa.map_bafu_np(rn))
        self.assertIsNone(pa.map_bafu_park(snp))
        self.assertEqual(pa.map_bafu_park(rn)["properties"]["kind"], "Regionaler Naturpark")
        self.assertEqual(pa.map_bafu_park(ne)["properties"]["kind"], "Naturerlebnispark")
        self.assertEqual(pa.map_bafu_park(ne)["properties"]["zone_class"], "protected")
        self.assertEqual(pa.map_bafu_park(ne)["properties"]["id"], "bafu-park:36")

    def test_identify_style_record_with_feature_id_in_properties_slot(self):
        record = {"type": "Feature", "featureId": 20, "properties": {"name": "X", "kategorie": "RN"}, "geometry": SQUARE}
        self.assertEqual(pa.map_bafu_park(record)["properties"]["id"], "bafu-park:20")

    def test_unknown_category_is_capitalised(self):
        zone = pa.map_bafu_park(self.park("xy", "A", 1))
        self.assertEqual(zone["properties"]["kind"], "Xy")

    def jagdbann(self, **overrides):
        properties = {
            "jb_name": "Combe-Grède (Nr. 2.00)", "schutzs_de": "rechtsverbindlich", "best_de": "Schneesportarten verboten ausserhalb markierter Pisten",
            "schutzzeit": "01.01.-31.12.", "zusatzinformation": "les chiens doivent être tenus en laisse", "url_kanton": "https://www.bafu.admin.ch/districts-francs",
        }
        properties.update(overrides)
        return feature(properties, fid=1245)

    def test_jagdbann_rule_text_binding_and_all_year(self):
        zone = pa.map_bafu_jagdbann(self.jagdbann())
        self.check_shape(zone)
        self.assertEqual(zone["properties"], {
            "id": "bafu-jagdbann:1245", "name": "Combe-Grède", "kind": "Jagdbanngebiet", "source": "bafu-schutzgebiete", "source_date": "",
            "binding": "binding", "link": "",
            "rule_text": "Schneesportarten verboten ausserhalb markierter Pisten\nles chiens doivent être tenus en laisse",
            "zone_class": "rules", "period_from": "", "period_to": "", "period_text": "",
        })

    def test_jagdbann_same_text_not_twice_and_recommended(self):
        zone = pa.map_bafu_jagdbann(self.jagdbann(zusatzinformation="SCHNEESPORTARTEN verboten ausserhalb markierter Pisten", schutzs_de="empfohlen"))
        self.assertEqual(zone["properties"]["rule_text"], "Schneesportarten verboten ausserhalb markierter Pisten")
        self.assertEqual(zone["properties"]["binding"], "recommended")
        self.assertEqual(pa.map_bafu_jagdbann(self.jagdbann(schutzs_de="anderes"))["properties"]["binding"], "")

    def test_jagdbann_period_and_unreadable_time(self):
        zone = pa.map_bafu_jagdbann(self.jagdbann(schutzzeit="20.12. - 30.04."))
        self.assertEqual((zone["properties"]["period_from"], zone["properties"]["period_to"], zone["properties"]["period_text"]), ("12-20", "04-30", "20.12. - 30.04."))
        zone = pa.map_bafu_jagdbann(self.jagdbann(schutzzeit="En cas de neige."))
        self.assertEqual((zone["properties"]["period_from"], zone["properties"]["period_text"]), ("", "En cas de neige."))

    def test_routes_lines_are_dropped(self):
        route = feature({"weg_id": 569, "wegtyp_de": "Erlaubter Weg / Route"}, geometry=LINE, fid=48256)
        self.assertIsNone(pa.map_bafu_jagdbann(route))
        self.assertIsNone(pa.map_bafu_np(feature({"kategorie": "SNP"}, geometry=LINE, fid=1)))

    def test_no_id_is_dropped(self):
        self.assertIsNone(pa.map_bafu_jagdbann(feature({"jb_name": "A"})))


class LfuBayern(Base):
    def test_every_type_has_its_class(self):
        expected = {
            "nationalpark": "rules", "naturschutzgebiet": "rules", "nationales_naturmonument": "rules", "naturdenkmal_flaechig": "rules",
            "landschaftsschutzgebiet": "protected", "naturpark": "protected", "biosphaerenreservat": "protected",
            "fauna_flora_habitat_gebiet": "protected", "vogelschutzgebiet": "protected", "landschaftsbestandteil_flaechig": "protected",
        }
        for type_name, zone_class in expected.items():
            zone = pa.map_lfu_bayern(feature({"GmlID": f"{type_name}.1", "name": "N"}))
            self.assertEqual(zone["properties"]["zone_class"], zone_class, type_name)

    def test_nature_reserve(self):
        zone = pa.map_lfu_bayern(feature({"GmlID": "naturschutzgebiet.516", "name": "Pfahl-Ruine Schwärzenberg", "status": "aktuell"}))
        self.check_shape(zone)
        self.assertEqual(zone["properties"], {
            "id": "lfu-bayern:naturschutzgebiet.516", "name": "Pfahl-Ruine Schwärzenberg", "kind": "Naturschutzgebiet", "source": "lfu-bayern",
            "source_date": "", "binding": "", "link": "", "rule_text": "", "zone_class": "rules",
            "period_from": "", "period_to": "", "period_text": "",
        })

    def test_natura_2000_uses_upper_case_name_and_link(self):
        link = "https://www.lfu.bayern.de/natur/natura2000/browse/info?id=5923-302"
        zone = pa.map_lfu_bayern(feature({"GmlID": "fauna_flora_habitat_gebiet.2363", "NAME": "Winterquartiere", "LINK": link}))
        self.assertEqual((zone["properties"]["name"], zone["properties"]["link"], zone["properties"]["kind"]), ("Winterquartiere", link, "FFH-Gebiet"))

    def test_biosphere_zone_in_kind_and_ids_stay_apart(self):
        a = pa.map_lfu_bayern(feature({"GmlID": "biosphaerenreservat.1", "name": "BR", "zone_nam": "Kernzone"}))
        b = pa.map_lfu_bayern(feature({"GmlID": "biosphaerenreservat.2", "name": "BR", "zone_nam": "Pflegezone"}))
        self.assertEqual(a["properties"]["kind"], "Biosphärenreservat (Kernzone)")
        self.assertNotEqual(a["properties"]["id"], b["properties"]["id"])

    def test_type_from_argument_or_feature_id(self):
        zone = pa.map_lfu_bayern(feature({"name": "A"}, fid="naturpark.4"))
        self.assertEqual((zone["properties"]["id"], zone["properties"]["kind"]), ("lfu-bayern:naturpark.4", "Naturpark"))
        zone = pa.map_lfu_bayern(feature({"GmlID": "x.1", "name": "A"}), type_name="vogelschutzgebiet")
        self.assertEqual(zone["properties"]["kind"], "Vogelschutzgebiet")

    def test_drops(self):
        self.assertIsNone(pa.map_lfu_bayern(feature({"GmlID": "unbekannt.1", "name": "A"})))
        self.assertIsNone(pa.map_lfu_bayern(feature({"name": "A"})))
        self.assertIsNone(pa.map_lfu_bayern(feature({"GmlID": "naturpark.4"}, geometry=None)))


class Vorarlberg(Base):
    def record(self, kategorie, **extra):
        properties = {"uuid": "fc2a4f8e", "name": "Benger-Park", "kategorie": kategorie, "verordnung": "§ 29",
                      "ris_link": "https://www.ris.bka.gv.at/GeltendeFassung.wxe?Abfrage=LrVbg"}
        properties.update(extra)
        return feature(properties)

    def test_rule_categories_are_rules_the_rest_outlines(self):
        for category in ["Naturschutzgebiet", "Pflanzenschutzgebiet", "Ruhezone"]:
            self.assertEqual(pa.map_vorarlberg(self.record(category))["properties"]["zone_class"], "rules", category)
        for category in ["Landschaftsschutzgebiet", "geschützter Landschaftsteil", "örtliches Schutzgebiet", "Biosphärenpark", "Naturpark", "sonstige Schutzgebiete"]:
            self.assertEqual(pa.map_vorarlberg(self.record(category))["properties"]["zone_class"], "protected", category)

    def test_fields_and_no_rule_text_from_the_legal_basis(self):
        zone = pa.map_vorarlberg(self.record("örtliches Schutzgebiet"))
        self.check_shape(zone)
        self.assertEqual(zone["properties"], {
            "id": "vorarlberg:fc2a4f8e", "name": "Benger-Park", "kind": "Örtliches Schutzgebiet", "source": "land-vorarlberg", "source_date": "",
            "binding": "", "link": "https://www.ris.bka.gv.at/GeltendeFassung.wxe?Abfrage=LrVbg", "rule_text": "", "zone_class": "protected",
            "period_from": "", "period_to": "", "period_text": "",
        })

    def test_drops_without_uuid_or_polygon(self):
        self.assertIsNone(pa.map_vorarlberg(self.record("Naturpark", uuid=None)))
        self.assertIsNone(pa.map_vorarlberg(feature({"uuid": "u"}, geometry={"type": "Point", "coordinates": [1, 2]})))


class Bozen(Base):
    def landscape(self, category, name, object_id=1):
        return feature({"OBJECTID": object_id, "BEZ_D": category, "NSO_DESC_DE": name}, fid="LandscapePlan-ProtectedAreas.%d" % object_id)

    def test_parks_national_park_rules_nature_park_outline(self):
        national = feature({"OBJECTID": 122460183, "BEZ_D": "Nationalpark", "NSO_DESC_DE": "Stilfserjoch", "NSO_LINK_DE": "https://www.nationalpark-stelvio.it/x.pdf"},
                           fid="LandscapePlan-NationalAndNaturalParks.122460183")
        zone = pa.map_bozen(national)
        self.check_shape(zone)
        self.assertEqual(zone["properties"], {
            "id": "bozen:parks:122460183", "name": "Stilfserjoch", "kind": "Nationalpark", "source": "provinz-bozen", "source_date": "",
            "binding": "", "link": "https://www.nationalpark-stelvio.it/x.pdf", "rule_text": "", "zone_class": "rules",
            "period_from": "", "period_to": "", "period_text": "",
        })
        nature = feature({"OBJECTID": 2, "BEZ_D": "Naturpark", "NSO_DESC_DE": "Drei Zinnen"}, fid="LandscapePlan-NationalAndNaturalParks.2")
        self.assertEqual(pa.map_bozen(nature)["properties"]["zone_class"], "protected")
        other = feature({"OBJECTID": 3, "BEZ_D": "Etwas anderes", "NSO_DESC_DE": "X"})
        self.assertIsNone(pa.map_bozen(other, type_key="parks"))

    def test_biotope_is_rules_and_natura_2000_is_outline(self):
        biotope = feature({"OBJECTID": 5, "BEZ_D": "Geschütztes Biotop", "NSO_DESC_DE": "Feuchtgebiet Hinteres Moos", "NSO_LINK_DE": "https://x/biotope.jsp?id=3656"},
                          fid="LandscapePlan-Biotopes.5")
        zone = pa.map_bozen(biotope)
        self.assertEqual((zone["properties"]["id"], zone["properties"]["kind"], zone["properties"]["zone_class"]), ("bozen:biotopes:5", "Geschütztes Biotop", "rules"))
        natura = feature({"OBJECTID": 6, "BEZ_D": "Felsrasen St. Ottilia", "BEZEICHNUNG_D": "Felsrasen St. Ottilia", "N2000_CODE": "IT3110043",
                          "N2000_CODE_LINK_DE": "https://natur-raum.provinz.bz.it/x"}, fid="LandscapePlan-Natura2000.6")
        zone = pa.map_bozen(natura)
        self.assertEqual((zone["properties"]["id"], zone["properties"]["kind"], zone["properties"]["zone_class"], zone["properties"]["link"]),
                         ("bozen:natura2000:6", "Natura-2000-Gebiet", "protected", "https://natur-raum.provinz.bz.it/x"))
        self.assertEqual(zone["properties"]["name"], "Felsrasen St. Ottilia")

    def test_natura_name_falls_back_to_category_field(self):
        natura = feature({"OBJECTID": 7, "BEZ_D": "Gebiet B", "N2000_CODE": "IT1"})
        self.assertEqual(pa.map_bozen(natura)["properties"]["name"], "Gebiet B")

    def test_landscape_keeps_only_named_landscape_protection_areas(self):
        kept = pa.map_bozen(self.landscape("Landschaftsschutzgebiet", "Spieler - Stoanerne Mandln", 1))
        self.assertEqual((kept["properties"]["kind"], kept["properties"]["zone_class"], kept["properties"]["name"], kept["properties"]["link"]),
                         ("Landschaftsschutzgebiet", "protected", "Spieler - Stoanerne Mandln", ""))
        self.assertEqual(kept["properties"]["id"], "bozen:landscape:1")
        self.assertIsNone(pa.map_bozen(self.landscape("Landschaftsschutzgebiet", None, 2)))
        self.assertIsNone(pa.map_bozen(self.landscape("Landschaftsschutzgebiet", "   ", 3)))
        for category in ["Landschaftliche Bannzone", "Gebiet mit Ensembleschutz", "Landschaftsschutzermächtigung durch die Landesverwaltung"]:
            self.assertIsNone(pa.map_bozen(self.landscape(category, "Mit Namen", 4)), category)

    def test_unknown_dataset_is_dropped_and_type_key_argument_works(self):
        self.assertIsNone(pa.map_bozen(feature({"OBJECTID": 1, "BEZ_D": "?"})))
        zone = pa.map_bozen(feature({"OBJECTID": 9, "BEZ_D": "Geschütztes Biotop", "NSO_DESC_DE": "A"}), type_key="LandscapePlan-Biotopes")
        self.assertEqual(zone["properties"]["id"], "bozen:biotopes:9")
        self.assertIsNone(pa.map_bozen(feature({"BEZ_D": "Naturpark", "NSO_DESC_DE": "A"}, fid="LandscapePlan-NationalAndNaturalParks.1")))


class LabelPointAndRegistry(unittest.TestCase):
    def test_label_point_inside_largest_part(self):
        small = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
        large = [[[10, 10], [14, 10], [14, 14], [10, 14], [10, 10]]]
        point = pa.label_point(feature({}, geometry={"type": "MultiPolygon", "coordinates": [small, large]}))
        self.assertTrue(10 < point[0] < 14 and 10 < point[1] < 14, point)

    def test_label_point_avoids_a_hole(self):
        outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        hole = [[1, 1], [9, 1], [9, 9], [1, 9], [1, 1]]
        point = pa.label_point(feature({}, geometry={"type": "Polygon", "coordinates": [outer, hole]}))
        in_hole = 1 < point[0] < 9 and 1 < point[1] < 9
        self.assertFalse(in_hole, point)

    def test_label_point_without_polygon(self):
        self.assertIsNone(pa.label_point(feature({}, geometry=LINE)))
        self.assertIsNone(pa.label_point(feature({}, geometry=None)))

    def test_registry(self):
        self.assertEqual(set(pa.SOURCES), {"bafu-np", "bafu-park", "bafu-jagdbann", "lfu-bayern", "vorarlberg", "bozen"})
        self.assertNotIn("bafu-wildlife", pa.SOURCES)
        self.assertNotIn("tirol", pa.SOURCES)
        for source, function in pa.SOURCES.items():
            self.assertIs(function, getattr(pa, "map_" + source.replace("-", "_")))

    def test_source_date_is_passed_through(self):
        zone = pa.map_vorarlberg(feature({"uuid": "u", "kategorie": "Naturpark"}), source_date="2026-11-25")
        self.assertEqual(zone["properties"]["source_date"], "2026-11-25")


class AuthorityDate(Base):
    def test_vorarlberg_takes_the_ordinance_date_of_the_record(self):
        zone = pa.map_vorarlberg(feature({"uuid": "u", "kategorie": "Naturpark", "lgdat": "1997-05-22"}))
        self.assertEqual(zone["properties"]["source_date"], "1997-05-22")

    def test_jagdbann_takes_the_year_of_the_decision(self):
        zone = pa.map_bafu_jagdbann(feature({"jb_name": "A (Nr. 1.00)", "beschlussjahr": 1991}, fid=1))
        self.assertEqual(zone["properties"]["source_date"], "1991")

    def test_no_date_in_the_record_stays_empty(self):
        for mapped in (pa.map_vorarlberg(feature({"uuid": "u", "kategorie": "Naturpark"})),
                       pa.map_bafu_jagdbann(feature({"jb_name": "A"}, fid=1)),
                       pa.map_lfu_bayern(feature({"GmlID": "naturpark.4", "name": "A"})),
                       pa.map_bozen(feature({"OBJECTID": 1, "BEZ_D": "Naturpark", "NSO_DESC_DE": "X"}), type_key="parks")):
            self.assertEqual(mapped["properties"]["source_date"], "")

    def test_garbage_is_no_date(self):
        for value in ("", None, "gestern", "99", "22.05.1997 x"):
            self.assertEqual(pa.authority_date(value), "")


if __name__ == "__main__":
    unittest.main()
