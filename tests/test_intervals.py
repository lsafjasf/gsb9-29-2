"""区间域推导单元测试。"""
import math
import unittest

from alerts_lint.intervals import Domain, predicate_region


def dom(*intervals, integer=False):
    return Domain(tuple(intervals), integer)


class TestDomainOps(unittest.TestCase):
    def test_intersect_basic(self):
        a = dom((0, True, 100, True))
        b = dom((50, True, None, False))
        self.assertEqual(a.intersect(b).intervals, ((50, True, 100, True),))

    def test_intersect_empty(self):
        a = dom((0, True, 10, True))
        b = dom((20, True, 30, True))
        self.assertTrue(a.intersect(b).is_empty())

    def test_intersect_open_touch_is_empty(self):
        a = dom((0, True, 10, False))
        b = dom((10, False, 20, True))
        self.assertTrue(a.intersect(b).is_empty())

    def test_intersect_closed_touch_is_point(self):
        a = dom((0, True, 10, True))
        b = dom((10, True, 20, True))
        self.assertEqual(a.intersect(b).intervals, ((10, True, 10, True),))

    def test_complement(self):
        universe = dom((0, True, 100, True))
        region = dom((90, False, None, False)).intersect(universe)
        rest = region.complement_within(universe)
        self.assertEqual(rest.intervals, ((0, True, 90, True),))

    def test_complement_splits(self):
        universe = dom((0, True, 100, True))
        point = dom((50, True, 50, True))
        rest = point.complement_within(universe)
        self.assertEqual(
            rest.intervals, ((0, True, 50, False), (50, False, 100, True))
        )

    def test_complement_full_is_empty(self):
        universe = dom((0, True, 100, True))
        self.assertTrue(universe.complement_within(universe).is_empty())

    def test_integer_count(self):
        d = dom((0, True, 100, True), integer=True)
        self.assertEqual(d.count(), 101)
        gt = predicate_region(">", 89, d)
        self.assertEqual(gt.count(), 11)  # 90..100

    def test_integer_open_bounds(self):
        d = dom((0, True, 10, True), integer=True)
        region = predicate_region(">=", 3, d).intersect(predicate_region("<", 8, d))
        self.assertEqual(region.count(), 5)  # 3,4,5,6,7

    def test_unbounded_measure_inf(self):
        d = dom((0, True, None, False))
        self.assertTrue(math.isinf(d.measure()))

    def test_jaccard(self):
        a = dom((90, False, 100, True))
        b = dom((80, False, 100, True))
        self.assertAlmostEqual(a.jaccard(b), 0.5)

    def test_jaccard_unbounded_returns_none(self):
        a = dom((90, False, None, False))
        b = dom((80, False, None, False))
        self.assertIsNone(a.jaccard(b))


class TestPredicateRegion(unittest.TestCase):
    def setUp(self):
        self.u = dom((0, True, 100, True))

    def test_gt_inside(self):
        r = predicate_region(">", 90, self.u)
        self.assertEqual(r.intervals, ((90, False, 100, True),))

    def test_gt_beyond_max_empty(self):
        self.assertTrue(predicate_region(">", 150, self.u).is_empty())

    def test_ge_at_max_is_point(self):
        r = predicate_region(">=", 100, self.u)
        self.assertEqual(r.intervals, ((100, True, 100, True),))

    def test_ge_at_min_is_full(self):
        r = predicate_region(">=", 0, self.u)
        self.assertEqual(r.intervals, self.u.intervals)

    def test_lt_below_min_empty(self):
        self.assertTrue(predicate_region("<", 0, self.u).is_empty())

    def test_le_below_min_empty(self):
        self.assertTrue(predicate_region("<=", -1, self.u).is_empty())

    def test_eq_outside_empty(self):
        self.assertTrue(predicate_region("==", 120, self.u).is_empty())

    def test_eq_inside_point(self):
        r = predicate_region("==", 50, self.u)
        self.assertEqual(r.intervals, ((50, True, 50, True),))

    def test_ne_outside_is_full(self):
        r = predicate_region("!=", 150, self.u)
        self.assertEqual(r.intervals, self.u.intervals)

    def test_ne_inside_splits(self):
        r = predicate_region("!=", 50, self.u)
        self.assertEqual(
            r.intervals, ((0, True, 50, False), (50, False, 100, True))
        )


if __name__ == "__main__":
    unittest.main()
