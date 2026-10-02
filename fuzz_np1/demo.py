"""Campaign driver: mutate, run under isolation, measure, minimise, persist.

Buckets
-------
* ``special`` - empty / header-only / 2 MiB valid / oversize length pin /
  depth-1500 nesting / legacy-sentinel hang;
* ``single``  - every enumerated structured mutation plus wire-level ops,
  once per seed;
* ``pairs``   - all 2-wise structured-mutation combinations on the small
  seeds (stride-capped, deterministic);
* ``random``  - seeded random pipelines (1-3 tree edits + 0-2 wire edits).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
from typing import Dict, Iterator, List, Optional, Tuple

from . import mutator
from .minimizer import minimize
from .protocol import (
    Document,
    Node,
    SEED_BUILDERS,
    TAG_BLOB,
    build_blob,
    build_deep,
    build_header_only,
    build_small,
    serialize_doc,
)
from .runner import (
    SafeRunner,
    STATUS_CRASH,
    STATUS_OK,
    STATUS_REJECT,
    STATUS_TIMEOUT,
)

ALL_BUG_CLASSES = ("length_desync", "depth_overflow", "oversize_alloc",
                   "hang_sentinel")

Labeled = Tuple[str, bytes, List[Tuple]]


def special_cases() -> List[Labeled]:
    oversize_pin = Document(
        children=[Node(TAG_BLOB, bytearray(b"AB"), pin_len=0xFFFFFFFF)])
    sentinel_data, sentinel_specs = mutator.sentinel_variant(build_small())
    return [
        ("empty_bytes", b"", []),
        ("header_only", serialize_doc(build_header_only()), []),
        ("oversize_valid_2MB", serialize_doc(build_blob(2 * 1024 * 1024)), []),
        ("oversize_len_pin", serialize_doc(oversize_pin),
         [("len_pin_abs", 0, 0xFFFFFFFF)]),
        ("deep_nesting_1500", serialize_doc(build_deep(1500)), []),
        ("legacy_sentinel_hang", sentinel_data, sentinel_specs),
    ]


def bucket_variants(bucket: str, rng_seed: int = 0,
                    pair_cap: Optional[int] = None,
                    random_count: int = 0) -> Iterator[Labeled]:
    if bucket == "special":
        yield from special_cases()
        return

    for name, builder in SEED_BUILDERS.items():
        doc = builder()
        if bucket == "single":
            for data, specs in mutator.single_doc_variants(doc):
                yield name, data, specs
            for data, specs in mutator.wire_variants(doc):
                yield name, data, specs
        elif bucket == "pairs" and name in ("small", "medium", "header_only"):
            for data, specs in mutator.pair_doc_variants(doc, cap=pair_cap):
                yield name, data, specs
        elif bucket == "random":
            rng = random.Random("%d:%s" % (rng_seed, name))
            for data, specs in mutator.random_variants(doc, rng, random_count):
                yield name, data, specs


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stream_hash(bucket: str, **kwargs) -> str:
    digest = hashlib.sha256()
    for _label, data, _specs in bucket_variants(bucket, **kwargs):
        digest.update(data)
    return digest.hexdigest()


def run_campaign(args: argparse.Namespace) -> dict:
    sys.setrecursionlimit(100000)  # deep-nesting seeds serialise recursively
    findings_dir = os.path.join(args.out, "findings")
    if os.path.isdir(findings_dir):
        shutil.rmtree(findings_dir)
    os.makedirs(findings_dir, exist_ok=True)

    bucket_kwargs: Dict[str, dict] = {
        "special": {},
        "single": {},
        "pairs": {"pair_cap": args.pair_cap},
        "random": {"rng_seed": args.seed, "random_count": args.random_count},
    }
    stats = {b: {STATUS_OK: 0, STATUS_REJECT: 0, STATUS_CRASH: 0,
                 STATUS_TIMEOUT: 0, "total": 0} for b in bucket_kwargs}
    findings: Dict[Tuple[str, str], dict] = {}

    with SafeRunner(timeout=args.timeout) as runner:
        stream_hashes: Dict[str, str] = {}
        for bucket, kwargs in bucket_kwargs.items():
            digest = hashlib.sha256()
            for label, data, specs in bucket_variants(bucket, **kwargs):
                digest.update(data)
                counters = stats[bucket]
                counters["total"] += 1
                status, cls, detail = runner.run(data)
                counters[status] = counters.get(status, 0) + 1
                if status in (STATUS_CRASH, STATUS_TIMEOUT) and \
                        (status, cls) not in findings:
                    findings[(status, cls)] = {
                        "status": status, "bug_class": cls, "bucket": bucket,
                        "seed": label, "size": len(data), "specs": specs,
                        "detail": detail, "data": data,
                    }
            stream_hashes[bucket] = digest.hexdigest()

        minimized_meta = []
        for _key, rec in sorted(findings.items()):
            is_timeout = rec["status"] == STATUS_TIMEOUT

            def oracle(candidate: bytes, rec=rec, is_timeout=is_timeout) -> bool:
                status, cls, _detail = runner.run(
                    candidate, timeout=0.12 if is_timeout else None)
                if is_timeout:
                    return status == STATUS_TIMEOUT
                return status == STATUS_CRASH and cls == rec["bug_class"]

            min_data, calls = minimize(
                rec["data"], oracle,
                max_tries=80 if is_timeout else 300,
                structured=not is_timeout)
            status, cls, detail = runner.run(min_data, timeout=args.timeout)
            reconfirmed = (status == STATUS_TIMEOUT) if is_timeout else (
                status == STATUS_CRASH and cls == rec["bug_class"])

            base = "%s__%s__%s" % (
                rec["bug_class"], rec["bucket"], rec["seed"])
            raw_path = os.path.join(findings_dir, base + ".bin")
            min_path = os.path.join(findings_dir, base + ".min.bin")
            with open(raw_path, "wb") as handle:
                handle.write(rec["data"])
            with open(min_path, "wb") as handle:
                handle.write(min_data)
            meta = {
                "bug_class": rec["bug_class"],
                "status": rec["status"],
                "first_seen_in": rec["bucket"],
                "seed": rec["seed"],
                "mutation_specs": rec["specs"],
                "original_size": rec["size"],
                "minimized_size": len(min_data),
                "oracle_calls": calls,
                "reconfirmed": reconfirmed,
                "sha256_min": _sha256(min_data),
                "raw_file": os.path.relpath(raw_path, args.out),
                "min_file": os.path.relpath(min_path, args.out),
                "min_hex": min_data.hex(),
                "detail": detail,
            }
            with open(os.path.join(findings_dir, base + ".json"), "w") as h:
                json.dump(meta, h, indent=2, sort_keys=True)
            minimized_meta.append(meta)

        # Second independent pass over every generator: identical streams
        # must be produced without any shared state.
        second_pass = {b: _stream_hash(b, **kwargs)
                       for b, kwargs in bucket_kwargs.items()}
        streams_reproducible = second_pass == stream_hashes

    detection = {}
    for bucket, counters in stats.items():
        total = counters["total"] or 1
        crashes = counters[STATUS_CRASH]
        timeouts = counters[STATUS_TIMEOUT]
        detection[bucket] = {
            "total": counters["total"],
            "ok": counters[STATUS_OK],
            "rejected": counters[STATUS_REJECT],
            "crashes": crashes,
            "timeouts": timeouts,
            "crash_rate": round(crashes / total, 4),
            "timeout_rate": round(timeouts / total, 4),
            "detection_rate": round((crashes + timeouts) / total, 4),
        }

    classes_found = sorted({rec["bug_class"] for rec in findings.values()})
    report = {
        "framework": "fuzz_np1",
        "master_seed": args.seed,
        "worker_timeout_s": args.timeout,
        "pair_cap": args.pair_cap,
        "random_count_per_seed": args.random_count,
        "buckets": detection,
        "unique_bug_classes_found": classes_found,
        "unique_bug_classes_total": len(ALL_BUG_CLASSES),
        "findings": minimized_meta,
        "reproducibility": {
            "variant_stream_sha256": stream_hashes,
            "second_pass_identical": streams_reproducible,
            "minimized_samples_reconfirmed":
                all(meta["reconfirmed"] for meta in minimized_meta),
        },
    }
    with open(os.path.join(args.out, "report.json"), "w") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)

    _print_summary(report)
    return report


def _print_summary(report: dict) -> None:
    print("== fuzz_np1 campaign (master seed %s) ==" % report["master_seed"])
    print("%-9s %6s %6s %6s %8s %9s %9s" % (
        "bucket", "total", "ok", "reject", "crashes", "timeouts", "det.rate"))
    for bucket, d in report["buckets"].items():
        print("%-9s %6d %6d %6d %8d %9d %9.2f%%" % (
            bucket, d["total"], d["ok"], d["rejected"], d["crashes"],
            d["timeouts"], d["detection_rate"] * 100))
    print("unique bug classes: %d/%d -> %s" % (
        len(report["unique_bug_classes_found"]),
        report["unique_bug_classes_total"],
        ", ".join(report["unique_bug_classes_found"])))
    repro = report["reproducibility"]
    print("variant streams reproducible: %s" % repro["second_pass_identical"])
    print("minimized samples reconfirmed: %s" %
          repro["minimized_samples_reconfirmed"])
    for meta in report["findings"]:
        print("  - %-15s %5d -> %4d bytes  %s" % (
            meta["bug_class"], meta["original_size"],
            meta["minimized_size"], meta["min_file"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Structure-aware NP1 mutation fuzzing campaign")
    parser.add_argument("--seed", type=int, default=20261003)
    parser.add_argument("--out", default="artifacts")
    parser.add_argument("--timeout", type=float, default=0.25)
    parser.add_argument("--pair-cap", type=int, default=None)
    parser.add_argument("--random-count", type=int, default=None)
    parser.add_argument("--quick", action="store_true",
                        help="smaller caps for a fast smoke run")
    args = parser.parse_args(argv)
    if args.pair_cap is None:
        args.pair_cap = 600 if args.quick else 4000
    if args.random_count is None:
        args.random_count = 60 if args.quick else 300
    run_campaign(args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
