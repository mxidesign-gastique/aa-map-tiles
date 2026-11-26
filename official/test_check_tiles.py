import unittest

from check_tiles import check


def decoded(*values):
    """The shape tippecanoe-decode prints: archive -> tile -> layer -> features."""
    features = [{"type": "Feature", "properties": {"source": "lfu-bayern", "approximate": v}, "geometry": None} for v in values]
    layer = {"type": "FeatureCollection", "properties": {"layer": "hazard_coverage", "version": 2, "extent": 4096}, "features": features}
    other = {"type": "FeatureCollection", "properties": {"layer": "route"}, "features": [{"type": "Feature", "properties": {"approximate": "x"}}]}
    tile = {"type": "FeatureCollection", "properties": {"zoom": 6, "x": 33, "y": 22}, "features": [layer, other]}
    return {"type": "FeatureCollection", "properties": {}, "features": [tile]}


class CheckTilesTest(unittest.TestCase):
    def test_numbers_zero_and_one_pass(self):
        self.assertEqual(check(decoded(0, 1, 1)), (3, []))

    def test_a_boolean_stops_the_run(self):
        seen, problems = check(decoded(0, True))
        self.assertEqual(seen, 2)
        self.assertEqual(len(problems), 1)
        self.assertIn("6/33/22: approximate=true (bool)", problems[0])
        self.assertEqual(len(check(decoded(False))[1]), 1)

    def test_a_foreign_string_or_other_number_stops_the_run(self):
        for bad in ("lfu-bayern:ghk_sturz_o_wald:1018014", "1", 2, 1.5, None):
            with self.subTest(bad=bad):
                self.assertEqual(len(check(decoded(bad))[1]), 1)

    def test_no_coverage_at_all_stops_the_run(self):
        seen, problems = check(decoded())
        self.assertEqual(seen, 0)
        self.assertIn("no hazard_coverage feature", problems[0])


if __name__ == "__main__":
    unittest.main()
