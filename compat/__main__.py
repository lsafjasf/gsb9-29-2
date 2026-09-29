"""命令行入口：

    python3 -m compat gen-samples [--dir samples]   生成/保留样例文件与密钥
    python3 -m compat matrix [--samples samples] [--out compat_matrix.md]
    python3 -m compat selftest
"""

import argparse
import os
import sys

from . import format_v1, format_v2, samples
from .framework import render_markdown, run_matrix

REFERENCE_READERS = {
    "v1 读取器": (1, format_v1.read_file),
    "v2 读取器": (2, format_v2.read_file),
}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="compat")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_gen = sub.add_parser("gen-samples", help="生成各版本样例文件与密钥")
    p_gen.add_argument("--dir", default="samples")
    p_mat = sub.add_parser("matrix", help="运行兼容矩阵并输出 Markdown")
    p_mat.add_argument("--samples", default="samples")
    p_mat.add_argument("--out", default="compat_matrix.md")
    sub.add_parser("selftest", help="框架自测")
    args = parser.parse_args(argv)

    if args.cmd == "gen-samples":
        samples.build_samples(args.dir)
        print("样例已生成于 %s/" % args.dir)
        return 0
    if args.cmd == "matrix":
        if not os.path.exists(os.path.join(args.samples, "keys.json")):
            samples.build_samples(args.samples)
        cases = samples.load_cases(args.samples)
        cells = run_matrix(REFERENCE_READERS, cases)
        md = render_markdown(cells, list(REFERENCE_READERS))
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(md)
        n_fail = sum(1 for c in cells if c.verdict != "PASS")
        print("矩阵已写入 %s；共 %d 格，FAIL %d 格。" % (args.out, len(cells), n_fail))
        return 1 if n_fail else 0
    if args.cmd == "selftest":
        from . import selftest
        return selftest.main()
    return 2


if __name__ == "__main__":
    sys.exit(main())
