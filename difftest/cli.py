"""命令行入口。

用法：
    python3 run_difftest.py [--cases N] [--seed S] [--out DIR]
                            [--impl-a module:func] [--impl-b module:func]
退出码：0 = 无差异；1 = 发现差异；2 = 用法/加载错误。
"""

import argparse
import importlib
import sys

from difftest.coverage import Coverage
from difftest.generator import Generator
from difftest.reducer import Reducer
from difftest.report import ReportWriter
from difftest.runner import DifferentialRunner


def _load(spec):
    module_name, _, func_name = spec.partition(":")
    if not module_name or not func_name:
        raise ValueError("实现指定格式应为 module:func，得到 %r" % spec)
    module = importlib.import_module(module_name)
    return getattr(module, func_name)


def build_arg_parser():
    ap = argparse.ArgumentParser(description="协议解析器差分测试框架")
    ap.add_argument("--cases", type=int, default=2000, help="随机用例数")
    ap.add_argument("--seed", type=int, default=1, help="随机种子（可复现）")
    ap.add_argument("--out", default="reports", help="报告输出目录")
    ap.add_argument("--impl-a", default="parsers.reference:parse",
                    help="参照实现，module:func")
    ap.add_argument("--impl-b", default="parsers.candidate:parse",
                    help="待测实现，module:func")
    ap.add_argument("--max-depth", type=int, default=32, help="协议嵌套上限")
    ap.add_argument("--oversized-threshold", type=int, default=70000,
                    help="超大报文字节阈值")
    ap.add_argument("--max-counterexamples", type=int, default=50,
                    help="最多落盘的最小反例数")
    ap.add_argument("--no-reduce", action="store_true", help="跳过失败归约")
    return ap


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        parse_a = _load(args.impl_a)
        parse_b = _load(args.impl_b)
    except (ValueError, ImportError, AttributeError) as exc:
        print("加载实现失败: %s" % exc, file=sys.stderr)
        return 2

    gen = Generator(seed=args.seed, max_depth=args.max_depth,
                    oversized_threshold=args.oversized_threshold)
    reducer = None if args.no_reduce else Reducer(parse_a, parse_b)
    runner = DifferentialRunner(parse_a, parse_b, reducer=reducer,
                                name_a=args.impl_a, name_b=args.impl_b)
    coverage = Coverage()

    for index, (data, tags) in enumerate(gen.cases(args.cases)):
        coverage.record(tags)
        runner.run_case(index, data, tags)

    writer = ReportWriter(args.out)
    stored = runner.divergences[:args.max_counterexamples]
    for seq, div in enumerate(stored, 1):
        writer.add_divergence(seq, div, args.impl_a, args.impl_b)
    report_path = writer.write_summary(runner.summary(), coverage.report())

    summary = runner.summary()
    print("用例总数: %d  一致: %d  差异: %d"
          % (summary["total"], summary["consistent"], summary["divergent"]))
    for verdict, count in summary["verdicts"].items():
        print("  %s: %d" % (verdict, count))
    print(coverage.format_text())
    if runner.divergences:
        print("最小反例已保存到 %s/ (共 %d 处差异，落盘 %d 个)"
              % (writer.ce_dir, len(runner.divergences), len(stored)))
    print("报告: %s" % report_path)
    return 1 if runner.divergences else 0


if __name__ == "__main__":
    sys.exit(main())
