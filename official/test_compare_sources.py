import unittest

from compare_sources import check

LIVE = {"sources": {"wlv-gzp": {"count": 900}, "lfu-bayern": {"count": 1000}}}


class CompareTest(unittest.TestCase):
    def test_normal_refresh_passes(self):
        self.assertEqual(check(LIVE, {"wlv-gzp": {"count": 905}, "lfu-bayern": {"count": 990}, "bvv-freizeitwege": {"count": 5}}), [])

    def test_a_source_that_failed_or_shrank_stops_the_run(self):
        problems = check(LIVE, {"wlv-gzp": {"count": 0}, "lfu-bayern": {"count": 500}})
        self.assertEqual(len(problems), 2)
        self.assertIn("missing", check(LIVE, {"wlv-gzp": {"count": 900}})[0])


if __name__ == "__main__":
    unittest.main()
