#!/usr/bin/env python3
"""interop_check / mytar 自测（仅标准库 unittest）。

运行：python3 -m unittest discover -s interop -v
"""

import os
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mytar
import interop_check as ic


class MytarRoundtripTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mytar-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.corpus = os.path.join(self.dir, "corpus")
        ic.build_corpus(self.corpus)

    def roundtrip(self, names=None):
        archive = os.path.join(self.dir, "out.tar")
        mytar.write_archive(archive, mytar.entries_from_tree(self.corpus, names))
        return mytar.read_archive(archive)

    def test_full_roundtrip(self):
        model = ic.model_from_tree(self.corpus)
        view = ic.View(*self._view(self.roundtrip()))
        self.assertEqual(ic.compare(model, view), [])

    def _view(self, entries):
        order, items = [], {}
        for e in entries:
            name = ic.norm_name(e.name, e.type)
            order.append(name)
            items[name] = ic.Item(e.mode, e.mtime, e.type,
                                  ic.sha256(e.data) if e.type == "file" else "",
                                  e.linkname)
        return order, items

    def test_long_filename_over_100_bytes(self):
        names = [e.name for e in self.roundtrip()]
        self.assertIn(ic.LONG_FILE, names)
        self.assertGreater(len(ic.LONG_FILE.encode("utf-8")), 100)

    def test_nonascii_name_and_content(self):
        entries = {e.name: e for e in self.roundtrip()}
        entry = entries[ic.NONASCII_FILE]
        self.assertEqual(entry.data, ic.CORPUS_FILES[ic.NONASCII_FILE])

    def test_sparse_file_content(self):
        entries = {e.name: e for e in self.roundtrip()}
        data = entries[ic.SPARSE_FILE].data
        self.assertEqual(len(data), ic.SPARSE_SIZE)
        self.assertTrue(data.startswith(b"SPARSE-HEAD"))
        self.assertTrue(data.endswith(b"SPARSE-TAIL"))
        self.assertEqual(data[16:-16], b"\0" * (ic.SPARSE_SIZE - 32))

    def test_empty_archive_roundtrip(self):
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        archive = os.path.join(self.dir, "empty.tar")
        mytar.write_archive(archive, mytar.entries_from_tree(empty))
        self.assertEqual(mytar.read_archive(archive), [])
        with tarfile.open(archive, "r") as tf:
            self.assertEqual(tf.getmembers(), [])

    def test_metadata_preserved(self):
        entries = {ic.norm_name(e.name, e.type): e for e in self.roundtrip()}
        self.assertEqual(entries["hello.txt"].mode, 0o640)
        self.assertEqual(entries["data"].mode, 0o750)
        self.assertEqual(int(entries["hello.txt"].mtime),
                         int(os.lstat(os.path.join(self.corpus, "hello.txt")).st_mtime))
        self.assertEqual(entries["link-to-hello"].type, "symlink")
        self.assertEqual(entries["link-to-hello"].linkname, "hello.txt")


class CrossToolReadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mytar-xtest-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.corpus = os.path.join(self.dir, "corpus")
        ic.build_corpus(self.corpus)

    def test_mytar_reads_tarfile_pax(self):
        archive = os.path.join(self.dir, "pax.tar")
        ic.write_tarfile(archive, self.corpus)
        names = [ic.norm_name(e.name, e.type) for e in mytar.read_archive(archive)]
        self.assertIn(ic.LONG_FILE, names)
        self.assertIn(ic.NONASCII_FILE, names)

    @unittest.skipUnless(shutil.which("tar"), "需要 GNU tar")
    def test_mytar_reads_gnutar(self):
        archive = os.path.join(self.dir, "gnu.tar")
        ic.write_gnutar(archive, self.corpus)
        model = ic.model_from_tree(self.corpus)
        view = ic.read_mytar(archive, self.dir)
        self.assertEqual(ic.compare(model, view), [])

    @unittest.skipUnless(shutil.which("tar"), "需要 GNU tar")
    def test_gnutar_reads_mytar(self):
        archive = os.path.join(self.dir, "mine.tar")
        ic.write_mytar(archive, self.corpus)
        model = ic.model_from_tree(self.corpus)
        view = ic.read_gnutar(archive, self.dir)
        self.assertEqual(ic.compare(model, view), [])


class CompareLogicTest(unittest.TestCase):
    def setUp(self):
        self.model = ic.View(["a.txt", "sub", "sub/b.txt"], {
            "a.txt": ic.Item(0o644, ic.FIXED_MTIME, "file", ic.sha256(b"aaa")),
            "sub": ic.Item(0o750, ic.FIXED_MTIME, "dir"),
            "sub/b.txt": ic.Item(0o600, ic.FIXED_MTIME, "file", ic.sha256(b"bbb")),
        })

    def view_with(self, *mutations, order=None):
        items = {k: ic.Item(v.mode, v.mtime, v.type, v.sha256, v.linkname)
                 for k, v in self.model.items.items()}
        for name, field, value in mutations:
            setattr(items[name], field, value)
        return ic.View(order if order is not None else list(self.model.order),
                       items)

    def fields(self, diffs):
        return {(d.entry, d.field) for d in diffs}

    def test_identical_passes(self):
        self.assertEqual(ic.compare(self.model, self.view_with()), [])

    def test_detects_content_diff(self):
        diffs = ic.compare(self.model, self.view_with(("a.txt", "sha256", "0" * 64)))
        self.assertIn(("a.txt", "content"), self.fields(diffs))

    def test_detects_order_diff(self):
        diffs = ic.compare(self.model,
                           self.view_with(order=["sub", "sub/b.txt", "a.txt"]))
        self.assertIn(("<archive>", "order"), self.fields(diffs))

    def test_detects_mode_diff(self):
        diffs = ic.compare(self.model, self.view_with(("a.txt", "mode", 0o777)))
        self.assertIn(("a.txt", "mode"), self.fields(diffs))

    def test_detects_mtime_diff(self):
        diffs = ic.compare(self.model,
                           self.view_with(("a.txt", "mtime", ic.FIXED_MTIME + 100)))
        self.assertIn(("a.txt", "mtime"), self.fields(diffs))

    def test_detects_type_diff(self):
        diffs = ic.compare(self.model, self.view_with(("sub", "type", "file")))
        self.assertIn(("sub", "type"), self.fields(diffs))

    def test_detects_missing_and_extra(self):
        view = self.view_with(order=["a.txt"])
        del view.items["sub"]
        del view.items["sub/b.txt"]
        fields = self.fields(ic.compare(self.model, view))
        self.assertIn(("sub", "presence"), fields)
        self.assertIn(("sub/b.txt", "presence"), fields)

    def test_evidence_is_locatable(self):
        diffs = ic.compare(self.model, self.view_with(("a.txt", "mode", 0o777)))
        text = str(diffs[0])
        self.assertIn("a.txt", text)
        self.assertIn("mode", text)
        self.assertIn("0o644", text)
        self.assertIn("0o777", text)

    def test_demo_evidence_nonempty(self):
        demos = ic.demo_evidence()
        self.assertGreaterEqual(len(demos), 3)
        for diff in demos:
            self.assertTrue(diff.entry)
            self.assertIn(diff.field, ic.FIELD_TO_DIMENSION)


@unittest.skipUnless(shutil.which("tar"), "需要 GNU tar")
class MatrixEndToEndTest(unittest.TestCase):
    def test_matrix_all_pass(self):
        workdir = tempfile.mkdtemp(prefix="interop-matrix-")
        self.addCleanup(shutil.rmtree, workdir, True)
        cells = ic.run_matrix(workdir)
        self.assertEqual(len(cells), len(ic.CASES) * 4)
        failures = [c for c in cells if c["status"] == "FAIL"]
        self.assertEqual(failures, [],
                         "\n".join(ic.render_evidence([c]) for c in failures))
        for case in ic.CASES:
            covered = {(c["writer"], c["reader"]) for c in cells if c["case"] == case}
            for other in ("gnu-tar", "py-tarfile"):
                self.assertIn(("mytar", other), covered)
                self.assertIn((other, "mytar"), covered)


if __name__ == "__main__":
    unittest.main()
