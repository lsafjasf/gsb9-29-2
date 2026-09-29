"""差异报告与最小反例落盘。"""

import json
import os


class ReportWriter:
    def __init__(self, outdir):
        self.outdir = outdir
        self.ce_dir = os.path.join(outdir, "counterexamples")
        os.makedirs(self.ce_dir, exist_ok=True)
        self.entries = []

    def add_divergence(self, seq, div, name_a, name_b):
        stem = "diff_%04d" % seq
        orig_path = os.path.join(self.ce_dir, stem + ".orig.bin")
        min_path = os.path.join(self.ce_dir, stem + ".min.bin")
        with open(orig_path, "wb") as fh:
            fh.write(div.data)
        minimized = div.minimized if div.minimized is not None else div.data
        with open(min_path, "wb") as fh:
            fh.write(minimized)
        out_a = div.min_out_a or div.out_a
        out_b = div.min_out_b or div.out_b
        entry = {
            "id": stem,
            "case_index": div.index,
            "verdict": div.verdict,
            "tags": sorted(div.tags),
            "original": {
                "path": os.path.relpath(orig_path, self.outdir),
                "size": len(div.data),
                "hex": div.data.hex(),
            },
            "minimized": {
                "path": os.path.relpath(min_path, self.outdir),
                "size": len(minimized),
                "hex": minimized.hex(),
            },
            "outputs": {
                name_a: out_a.public(),
                name_b: out_b.public(),
            },
        }
        meta_path = os.path.join(self.ce_dir, stem + ".json")
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False, indent=2)
        entry["meta"] = os.path.relpath(meta_path, self.outdir)
        self.entries.append(entry)
        return entry

    def write_summary(self, summary, coverage_report, extra=None):
        doc = {
            "summary": summary,
            "coverage": coverage_report,
            "divergences": self.entries,
        }
        if extra:
            doc.update(extra)
        path = os.path.join(self.outdir, "diff_report.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        return path
