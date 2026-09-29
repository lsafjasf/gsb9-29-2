#!/usr/bin/env python3
"""模糊测试入口。

示例：
  python3 fuzz/run_fuzz.py --seed 1 --iterations 500 --mode struct --parser buggy \
      --out results/struct_buggy
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Campaign, save_report


def main(argv=None):
    ap = argparse.ArgumentParser(description="CAFE-TLV 结构感知模糊测试")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--iterations", type=int, default=500)
    ap.add_argument("--mode", choices=["struct", "random"], default="struct",
                    help="struct=结构感知变异; random=纯随机字节翻转对照")
    ap.add_argument("--parser", choices=["buggy", "hardened"], default="buggy")
    ap.add_argument("--timeout", type=float, default=2.0,
                    help="单样本超时（秒）")
    ap.add_argument("--max-size", type=int, default=1 << 20,
                    help="生成报文大小上限（字节），超出则跳过")
    ap.add_argument("--out", default=None, help="输出目录（报告与崩溃样本）")
    ap.add_argument("--no-minimize", action="store_true",
                    help="不对崩溃样本做最小化")
    ap.add_argument("--dry-run", action="store_true",
                    help="只生成不执行（用于可复现性校验）")
    ap.add_argument("--progress", action="store_true")
    args = ap.parse_args(argv)

    campaign = Campaign(
        seed=args.seed,
        mode=args.mode,
        hardened=args.parser == "hardened",
        iterations=args.iterations,
        timeout=args.timeout,
        max_size=args.max_size,
        out_dir=args.out,
        do_minimize=not args.no_minimize,
    )
    report = campaign.run(execute=not args.dry_run, progress=args.progress)

    if args.out:
        path = save_report(report, args.out)
        print(f"report: {path}")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
