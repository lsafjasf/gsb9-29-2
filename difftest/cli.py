"""命令行入口。

用法：
    python3 -m difftest --adapter examples/toy_adapter.py
"""
from __future__ import annotations

import argparse
import importlib.util

from .campaign import Campaign, DiffConfig
from .harness import CATEGORY_LABELS


def load_adapter(path):
    spec = importlib.util.spec_from_file_location("difftest_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [name for name in ("SPEC", "parse_a", "parse_b") if not hasattr(module, name)]
    if missing:
        raise SystemExit(f"适配器缺少导出: {', '.join(missing)}（需要 SPEC/parse_a/parse_b）")
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="difftest",
        description="协议解析器差分测试框架（生成-对比-归约-报告）",
    )
    parser.add_argument("--adapter", required=True, help="适配器文件（定义 SPEC/parse_a/parse_b）")
    parser.add_argument("--cases", type=int, default=500, help="随机用例数（默认 500）")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", default="difftest_out", help="报告输出目录")
    parser.add_argument("--gen-max-depth", type=int, default=6, dest="gen_max_depth")
    parser.add_argument("--nesting-depth", type=int, default=12, dest="nesting_depth")
    parser.add_argument("--deep-depth", type=int, default=100, dest="deep_depth")
    parser.add_argument("--oversize-bytes", type=int, default=5000, dest="oversize_bytes")
    parser.add_argument("--shrink-budget", type=int, default=4000, dest="shrink_budget")
    args = parser.parse_args(argv)

    module = load_adapter(args.adapter)
    cfg = DiffConfig(
        cases=args.cases,
        seed=args.seed,
        outdir=args.out,
        gen_max_depth=args.gen_max_depth,
        nesting_depth=args.nesting_depth,
        deep_depth=args.deep_depth,
        oversize_bytes=args.oversize_bytes,
        shrink_budget=args.shrink_budget,
    )

    campaign = Campaign(module.SPEC, module.parse_a, module.parse_b, cfg)
    report = campaign.run()

    s = report["summary"]
    print(
        f"用例总数 {s['total_cases']}：结果一致 {s['match']}，"
        f"一边成功一边失败 {s['outcome_mismatch']}，"
        f"错误类别不同 {s['error_mismatch']}，结果值不同 {s['value_mismatch']}"
    )
    print(f"去重后差异签名 {s['unique_mismatch_signatures']} 个：")
    for m in report["mismatches"]:
        print(
            f"  [{m['id']}] {CATEGORY_LABELS[m['category']]}  "
            f"{m['original_len']}B -> {m['minimized_len']}B  "
            f"A={_fmt(m['output_a'])}  B={_fmt(m['output_b'])}  "
            f"{m['minimized_file']}"
        )
    for hint in report["hints"]:
        print(f"提示: {hint}")
    print(f"报告已写入 {args.out}/report.json 与 {args.out}/report.txt")
    return 1 if report["mismatches"] else 0


def _fmt(outcome):
    if outcome["ok"]:
        return "ok"
    return outcome["error"]


if __name__ == "__main__":
    raise SystemExit(main())
