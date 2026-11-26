"""Tests for the data date of a download (build_official.date_of, download_date)."""

import pathlib
import tempfile
import unittest
from datetime import date

from build_official import build_meta, date_of, download_date


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


if __name__ == "__main__":
    unittest.main()
