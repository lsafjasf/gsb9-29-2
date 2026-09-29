#!/usr/bin/env python3
"""兼容矩阵命令行入口。

用法：
  python3 run_compat.py matrix                 # 运行矩阵，终端输出
  python3 run_compat.py matrix --md out.md     # 同时写 Markdown 报告
  python3 run_compat.py matrix --json out.json # 同时写 JSON 报告
  python3 run_compat.py gen-fixtures           # 生成/重置样例文件与密钥
  python3 run_compat.py selftest               # 运行框架自测（含错误实现检测）

退出码：0=全部符合预期；1=存在不符合预期的格子或转换路径失败。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compat import fixtures, matrix
from compat.report import render_text, render_markdown, render_json


def cmd_matrix(args) -> int:
    cells, conversion = matrix.run()
    text = render_text(cells, conversion)
    print(text)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(cells, conversion))
        print(f"\n[报告] Markdown 已写入 {args.md}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(render_json(cells, conversion))
        print(f"[报告] JSON 已写入 {args.json}")
    bad = [c for c in cells if not c.matched]
    return 0 if not bad and conversion.ok else 1


def cmd_gen(args) -> int:
    fixtures.generate(force=True)
    print("样例文件、密钥与场景变体已重新生成于 fixtures/")
    return 0


def cmd_selftest(args) -> int:
    import unittest
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(os.path.dirname(__file__), "tests"),
                            pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=args.verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="加密文件格式跨版本兼容性测试框架")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_matrix = sub.add_parser("matrix", help="运行兼容矩阵")
    p_matrix.add_argument("--md", help="Markdown 报告输出路径")
    p_matrix.add_argument("--json", help="JSON 报告输出路径")
    p_matrix.set_defaults(func=cmd_matrix)

    p_gen = sub.add_parser("gen-fixtures", help="（重新）生成样例文件与密钥")
    p_gen.set_defaults(func=cmd_gen)

    p_self = sub.add_parser("selftest", help="运行框架自测")
    p_self.add_argument("-v", "--verbosity", type=int, default=2)
    p_self.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
