"""Tests for the data date of a download (build_official.date_of, download_date)."""

import pathlib
import tempfile
import unittest
from datetime import date

from build_official import COVERAGE_KEYS, build_meta, coverage_feature, date_of, download_date


class DownloadDateTest(unittest.TestCase):
    def test_last_modified_becomes_a_day(self):
        self.assertEqual(date_of("Mon, 23 Nov 2026 04:12:00 GMT"), "2026-11-23")

    def test_missing_or_broken_header_gives_nothing(self):
        self.assertEqual(date_of(None), "")
        self.assertEqual(date_of("yesterday"), "")

    def test_noted_date_else_the_day_of_the_build(self):
        with tempfile.TemporaryDirectory() as work:
            archive = pathlib.Path(work) / "by_basisdlm_plus.zip"
            self.assertEqual(download_date(archive), date.today().isoformat())
            (pathlib.Path(work) / "by_basisdlm_plus.zip.date").write_text("2026-11-23\n")
            self.assertEqual(download_date(archive), "2026-11-23")


class MetaTest(unittest.TestCase):
    def test_bolzano_without_a_date_is_not_stamped_with_the_build_day(self):
        meta = build_meta({"provinz-bozen", "wlv-gzp"}, {"provinz-bozen": 5, "wlv-gzp": 2}, {"wlv-gzp": "2026-05-01"})
        self.assertIsNone(meta["provinz-bozen"]["updated"])
        self.assertEqual(meta["provinz-bozen"]["count"], 5)
        self.assertEqual(meta["wlv-gzp"]["updated"], "2026-05-01")

    def test_protected_source_without_a_date_has_none(self):
        meta = build_meta(set(), {"protected:lfu-bayern": 3, "protected:vorarlberg": 2}, {"protected:vorarlberg": "2019-01-01"})
        self.assertIsNone(meta["protected:lfu-bayern"]["updated"])
        self.assertEqual(meta["protected:vorarlberg"]["updated"], "2019-01-01")


class CoverageTest(unittest.TestCase):
    GEOMETRY = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}

    def test_every_source_has_the_same_attributes_and_types(self):
        hint = coverage_feature(self.GEOMETRY, "lfu-bayern", "hint")["properties"]
        plan = coverage_feature(self.GEOMETRY, "provinz-bozen", "plan", "Jenesien", approximate=True)["properties"]
        for props in (hint, plan):
            self.assertEqual(tuple(props), COVERAGE_KEYS)
            self.assertIsInstance(props["approximate"], bool)
            self.assertIsInstance(props["municipality"], str)
        self.assertIs(hint["approximate"], False)
        self.assertIs(plan["approximate"], True)

    def test_approximate_never_carries_a_zone_id(self):
        props = coverage_feature(self.GEOMETRY, "wlv-gzp", "plan", "X", approximate="lfu-bayern:ghk_sturz_o_wald:1018014")["properties"]
        self.assertIs(props["approximate"], True)


if __name__ == "__main__":
    unittest.main()
