"""Self-tests: differential testing vs brute force, consistency assertions,
and edge cases (empty index, single point, all-same-position, frequent moves).

Run:  python3 test_spatial_index.py  (or: python3 -m unittest -v)
"""

import random
import unittest

from spatial_index import SpatialDB


def brute_rect(records, mins, maxs):
    return {k for k, (p, _) in records.items()
            if all(lo <= x <= hi for lo, hi, x in zip(mins, maxs, p))}


def brute_knn(records, q, k):
    return sorted(records,
                  key=lambda key: (sum((a - b) ** 2 for a, b in
                                       zip(records[key][0], q)), key))[:k]


def brute_key_range(records, lo, hi):
    return sorted(k for k in records if lo <= k <= hi)


class KeyQueryTests(unittest.TestCase):
    def setUp(self):
        random.seed(42)
        self.db = SpatialDB(dim=2)
        self.pts = {}
        for i in range(500):
            p = (random.random(), random.random())
            self.db.insert(i, p)
            self.pts[i] = (p, None)

    def test_eq_matches_brute(self):
        for key in [0, 250, 499, 500, -1]:
            got, stats = self.db.key_eq(key)
            want = [key] if key in self.pts else []
            self.assertEqual([k for k, _ in got], want)

    def test_range_matches_brute(self):
        for lo, hi in [(0, 99), (100, 300), (400, 1000), (-5, -1), (250, 250)]:
            got, _ = self.db.key_range(lo, hi)
            self.assertEqual([k for k, _ in got], brute_key_range(self.pts, lo, hi))

    def test_key_index_stays_in_sync_after_updates(self):
        for i in range(0, 500, 2):
            self.db.delete(i)
            del self.pts[i]
        for i in range(1000, 1100):
            p = (random.random(), random.random())
            self.db.insert(i, p)
            self.pts[i] = (p, None)
        self.db.assert_consistent()
        got, _ = self.db.key_range(0, 2000)
        self.assertEqual([k for k, _ in got], sorted(self.pts))


class SpatialQueryTests(unittest.TestCase):
    def test_rect_and_knn_match_brute_force(self):
        random.seed(7)
        for dim in (2, 3, 5):
            db = SpatialDB(dim=dim, max_entries=8)
            pts = {}
            for i in range(800):
                p = tuple(random.random() for _ in range(dim))
                db.insert(i, p)
                pts[i] = (p, None)
            db.assert_consistent()
            for _ in range(100):
                lo, hi = [], []
                for _ in range(dim):
                    a, b = sorted((random.random(), random.random()))
                    lo.append(a)
                    hi.append(b)
                got, stats = db.rect_query(lo, hi)
                self.assertEqual(set(got), brute_rect(pts, lo, hi))
                self.assertEqual(stats.candidates >= len(got), True)
            for _ in range(100):
                q = tuple(random.random() for _ in range(dim))
                k = random.randint(1, 30)
                got, _ = db.knn(q, k)
                self.assertEqual(got, brute_knn(pts, q, k))

    def test_updates_keep_index_consistent(self):
        random.seed(11)
        db = SpatialDB(dim=2, max_entries=8)
        pts = {}
        keys = list(range(600))
        for i in keys:
            p = (random.random(), random.random())
            db.insert(i, p)
            pts[i] = (p, None)
        for step in range(400):
            op = random.random()
            if op < 0.35 and pts:
                victim = random.choice(list(pts))
                db.delete(victim)
                del pts[victim]
            elif op < 0.7 and pts:
                mover = random.choice(list(pts))
                p = (random.random(), random.random())
                db.move(mover, p)
                pts[mover] = (p, None)
            else:
                fresh = max(pts, default=-1) + 1
                p = (random.random(), random.random())
                db.insert(fresh, p)
                pts[fresh] = (p, None)
            if step % 25 == 0:
                db.assert_consistent()
        db.assert_consistent()
        for _ in range(50):
            x1, x2 = sorted((random.random(), random.random()))
            y1, y2 = sorted((random.random(), random.random()))
            got, _ = db.rect_query((x1, y1), (x2, y2))
            self.assertEqual(set(got), brute_rect(pts, (x1, y1), (x2, y2)))
        for _ in range(50):
            q = (random.random(), random.random())
            k = random.randint(1, 15)
            got, _ = db.knn(q, k)
            self.assertEqual(got, brute_knn(pts, q, k))


class EdgeCaseTests(unittest.TestCase):
    def test_empty_index(self):
        db = SpatialDB(dim=2)
        db.assert_consistent()
        got, stats = db.rect_query((0, 0), (1, 1))
        self.assertEqual(got, [])
        self.assertEqual(stats.candidates, 0)
        got, stats = db.knn((0.5, 0.5), 5)
        self.assertEqual(got, [])
        got, _ = db.key_eq(1)
        self.assertEqual(got, [])
        got, _ = db.key_range(0, 100)
        self.assertEqual(got, [])
        with self.assertRaises(KeyError):
            db.delete(1)
        with self.assertRaises(KeyError):
            db.move(1, (0, 0))

    def test_single_point(self):
        db = SpatialDB(dim=3)
        db.insert("a", (1.0, 2.0, 3.0), payload="hello")
        db.assert_consistent()
        got, _ = db.rect_query((0, 0, 0), (2, 3, 4))
        self.assertEqual(got, ["a"])
        got, _ = db.rect_query((5, 5, 5), (9, 9, 9))
        self.assertEqual(got, [])
        got, _ = db.knn((0.0, 0.0, 0.0), 10)
        self.assertEqual(got, ["a"])
        rec, _ = db.key_eq("a")
        self.assertEqual(rec[0][1][1], "hello")
        db.delete("a")
        db.assert_consistent()
        got, _ = db.knn((0.0, 0.0, 0.0), 1)
        self.assertEqual(got, [])

    def test_all_same_position(self):
        db = SpatialDB(dim=2, max_entries=8)
        n = 100
        for i in range(n):
            db.insert(i, (0.5, 0.5))
        db.assert_consistent()
        got, stats = db.rect_query((0.0, 0.0), (1.0, 1.0))
        self.assertEqual(set(got), set(range(n)))
        self.assertEqual(stats.candidates, n)
        got, _ = db.rect_query((0.6, 0.6), (1.0, 1.0))
        self.assertEqual(got, [])
        got, _ = db.knn((0.5, 0.5), 10)
        self.assertEqual(len(got), 10)
        self.assertTrue(all(isinstance(k, int) and 0 <= k < n for k in got))
        for i in range(0, n, 2):
            db.delete(i)
        db.assert_consistent()
        got, _ = db.rect_query((0.0, 0.0), (1.0, 1.0))
        self.assertEqual(set(got), set(range(1, n, 2)))

    def test_frequent_moves(self):
        random.seed(99)
        db = SpatialDB(dim=2, max_entries=8)
        pts = {}
        for i in range(100):
            p = (random.random(), random.random())
            db.insert(i, p)
            pts[i] = (p, None)
        for step in range(2000):
            i = random.randrange(100)
            p = (random.random(), random.random())
            db.move(i, p)
            pts[i] = (p, None)
            if step % 100 == 0:
                db.assert_consistent()
        db.assert_consistent()
        for _ in range(50):
            x1, x2 = sorted((random.random(), random.random()))
            y1, y2 = sorted((random.random(), random.random()))
            got, _ = db.rect_query((x1, y1), (x2, y2))
            self.assertEqual(set(got), brute_rect(pts, (x1, y1), (x2, y2)))
        for _ in range(50):
            q = (random.random(), random.random())
            got, _ = db.knn(q, 5)
            self.assertEqual(got, brute_knn(pts, q, 5))

    def test_duplicate_insert_rejected(self):
        db = SpatialDB(dim=2)
        db.insert(1, (0.1, 0.1))
        with self.assertRaises(KeyError):
            db.insert(1, (0.2, 0.2))
        db.assert_consistent()
        self.assertEqual(len(db), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
