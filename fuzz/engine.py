"""种子驱动的模糊测试引擎。

可复现性：所有随机选择均来自 random.Random(seed)；同一 seed + 同一语料
产生的报文序列完全一致（sequence_sha256 相同）。
"""

import collections
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile

from message import Field, Message, TAG_CONTAINER, TAG_INT, TAG_RAW, TAG_STR
from minimize import minimize
from mutators import RANDOM_MUTATORS, STRUCT_MUTATORS

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PARSER = os.path.join(HERE, "parser_under_test.py")

CATEGORY_BY_RC = {0: "ok", 10: "reject_header", 11: "reject_field",
                  12: "reject_semantic"}
EXECUTED_CATEGORIES = ("ok", "reject_header", "reject_field",
                       "reject_semantic", "crash", "timeout")

_ERROR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b")


def default_corpus():
    deep = Field(TAG_INT, 7)
    for _ in range(256):
        deep = Field(TAG_CONTAINER, [deep])
    return [
        Message([], flags=0x00),
        Message([Field(TAG_INT, 42), Field(TAG_STR, b"hello")], flags=0x01),
        Message([
            Field(TAG_INT, 1),
            Field(TAG_CONTAINER, [
                Field(TAG_INT, 2),
                Field(TAG_STR, b"x"),
                Field(TAG_CONTAINER, [Field(TAG_RAW, b"\x00" * 8)]),
            ]),
        ], flags=0x01),
        Message([Field(TAG_RAW, b"\xab" * 32), Field(TAG_STR, b"tail")],
                flags=0x00),
        Message([Field(TAG_INT, 0), deep], flags=0x01),
    ]


def run_parser(parser_path, hardened, data, timeout):
    """在子进程中运行被测解析器，返回 (rc, stderr, timed_out)。"""
    fd, path = tempfile.mkstemp(suffix=".bin", prefix="fuzz_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        cmd = [sys.executable, parser_path]
        if hardened:
            cmd.append("--hardened")
        cmd.append(path)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            return proc.returncode, proc.stderr.decode("utf-8", "replace"), False
        except subprocess.TimeoutExpired:
            return None, "", True
    finally:
        os.unlink(path)


def extract_signature(stderr, rc):
    if rc is not None and rc < 0:
        return f"signal_{-rc}"
    lines = [l.strip() for l in (stderr or "").strip().splitlines() if l.strip()]
    for line in reversed(lines):
        match = _ERROR_RE.match(line)
        if match:
            return match.group(1)
    return f"rc_{rc}"


def classify(rc, stderr, timed_out):
    """返回 (category, signature)。"""
    if timed_out:
        return "timeout", "timeout"
    if rc in CATEGORY_BY_RC:
        return CATEGORY_BY_RC[rc], ""
    return "crash", extract_signature(stderr, rc)


class Campaign:
    def __init__(self, seed=1, mode="struct", hardened=False, iterations=100,
                 timeout=2.0, max_size=1 << 20, out_dir=None,
                 do_minimize=True, parser_path=None, corpus=None):
        if mode not in ("struct", "random"):
            raise ValueError(f"unknown mode: {mode}")
        self.seed = seed
        self.mode = mode
        self.hardened = hardened
        self.iterations = iterations
        self.timeout = timeout
        self.max_size = max_size
        self.out_dir = out_dir
        self.do_minimize = do_minimize
        self.parser_path = parser_path or DEFAULT_PARSER
        self.rng = random.Random(seed)
        self.mutators = STRUCT_MUTATORS if mode == "struct" else RANDOM_MUTATORS
        self.corpus = list(corpus) if corpus is not None else default_corpus()
        self.counts = collections.Counter()
        self.signatures = collections.Counter()
        self.seq_hash = hashlib.sha256()
        self.max_depth_generated = 0
        self.max_depth_accepted = 0
        self.crashes = []
        if out_dir:
            os.makedirs(os.path.join(out_dir, "crashes"), exist_ok=True)

    def generate(self):
        """生成一个变异报文，返回 (bytes, Message)。"""
        msg = self.rng.choice(self.corpus).clone()
        posts = []
        for _ in range(self.rng.randint(1, 4)):
            mut = self.rng.choice(self.mutators)
            if mut.is_post:
                posts.append(mut)
            else:
                mut.mutate(msg, self.rng)
        from message import serialize_with_layout
        data, offsets = serialize_with_layout(msg.fields, msg.flags)
        for post in posts:
            data = post.mutate_bytes(data, offsets, self.rng)
        return data, msg

    def execute(self, data):
        rc, stderr, timed_out = run_parser(self.parser_path, self.hardened,
                                           data, self.timeout)
        return classify(rc, stderr, timed_out)

    def _signature_of(self, data):
        _, sig = self.execute(data)
        return sig

    def _record_crash(self, data, category, sig):
        if any(c["signature"] == sig for c in self.crashes):
            return
        saved = data
        if category == "crash" and self.do_minimize:
            saved = minimize(data, lambda d: self._signature_of(d) == sig)
        fname = f"crash_{len(self.crashes):03d}_{_sanitize(sig)}.bin"
        path = None
        if self.out_dir:
            path = os.path.join(self.out_dir, "crashes", fname)
            with open(path, "wb") as fh:
                fh.write(saved)
        self.crashes.append({
            "signature": sig,
            "category": category,
            "file": path,
            "original_size": len(data),
            "minimized_size": len(saved),
        })

    def run(self, execute=True, progress=False):
        for i in range(self.iterations):
            data, msg = self.generate()
            self.seq_hash.update(data)
            depth = msg.depth()
            self.max_depth_generated = max(self.max_depth_generated, depth)
            if len(data) > self.max_size:
                self.counts["oversize_skip"] += 1
                continue
            if not execute:
                self.counts["generated"] += 1
                continue
            category, sig = self.execute(data)
            self.counts[category] += 1
            if category == "ok":
                self.max_depth_accepted = max(self.max_depth_accepted, depth)
                if len(self.corpus) < 256:
                    self.corpus.append(msg)
            elif category in ("crash", "timeout"):
                self.signatures[sig] += 1
                self._record_crash(data, category, sig)
            if progress and (i + 1) % 100 == 0:
                print(f"  [{i + 1}/{self.iterations}] {dict(self.counts)}",
                      flush=True)
        return self.report()

    def report(self):
        executed = sum(self.counts[c] for c in EXECUTED_CATEGORIES)
        detections = self.counts["crash"] + self.counts["timeout"]
        deep = executed - self.counts["reject_header"]
        return {
            "seed": self.seed,
            "mode": self.mode,
            "parser": "hardened" if self.hardened else "buggy",
            "iterations": self.iterations,
            "executed": executed,
            "counts": dict(self.counts),
            "detections": detections,
            "detection_rate": detections / executed if executed else 0.0,
            "unique_signatures": dict(self.signatures),
            "reached_field_stage_rate": deep / executed if executed else 0.0,
            "max_depth_generated": self.max_depth_generated,
            "max_depth_accepted": self.max_depth_accepted,
            "sequence_sha256": self.seq_hash.hexdigest(),
            "crashes": self.crashes,
        }


def _sanitize(sig):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sig)


def save_report(report, out_dir):
    path = os.path.join(out_dir, "report.json")
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    return path
