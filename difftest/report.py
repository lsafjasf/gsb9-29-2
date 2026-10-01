"""差异与最小反例报告。

输出目录结构：
    report.json          机器可读汇总（配置/统计/覆盖率/提示/差异列表）
    report.txt           人类可读摘要
    cases/mXXXX.orig.bin 差异原始样本
    cases/mXXXX.min.bin  归约后的最小反例
    cases/mXXXX.json     该差异的详情与两个实现各自的输出
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

from .harness import CATEGORY_LABELS, MATCH


def write_report(outdir, mismatches, coverage, cfg):
    os.makedirs(outdir, exist_ok=True)
    cases_dir = os.path.join(outdir, "cases")
    if mismatches:
        os.makedirs(cases_dir, exist_ok=True)

    entries = []
    for index, m in enumerate(mismatches, start=1):
        mid = f"m{index:04d}"
        orig_path = os.path.join(cases_dir, f"{mid}.orig.bin")
        min_path = os.path.join(cases_dir, f"{mid}.min.bin")
        with open(orig_path, "wb") as fh:
            fh.write(m.original)
        with open(min_path, "wb") as fh:
            fh.write(m.minimized)
        entry = {
            "id": mid,
            "category": m.category,
            "category_label": CATEGORY_LABELS[m.category],
            "strategy": m.tag,
            "original_len": len(m.original),
            "minimized_len": len(m.minimized),
            "original_file": os.path.relpath(orig_path, outdir),
            "minimized_file": os.path.relpath(min_path, outdir),
            "minimized_hex": m.minimized.hex(),
            "output_a": m.out_a.to_json(),
            "output_b": m.out_b.to_json(),
        }
        with open(os.path.join(cases_dir, f"{mid}.json"), "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False, indent=2)
        entries.append(entry)

    summary = {
        "total_cases": coverage.total,
        "match": coverage.categories.get(MATCH, 0),
        "value_mismatch": coverage.categories.get("VALUE_MISMATCH", 0),
        "outcome_mismatch": coverage.categories.get("OUTCOME_MISMATCH", 0),
        "error_mismatch": coverage.categories.get("ERROR_MISMATCH", 0),
        "unique_mismatch_signatures": len(mismatches),
    }

    report = {
        "config": asdict(cfg),
        "summary": summary,
        "coverage": coverage.to_json(),
        "hints": coverage.hints(),
        "mismatches": entries,
    }

    with open(os.path.join(outdir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(outdir, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(_render_text(report))
    return report


def _render_text(report):
    s = report["summary"]
    lines = [
        "差分测试报告",
        "=" * 60,
        f"用例总数: {s['total_cases']}  结果一致: {s['match']}",
        f"一边成功一边失败: {s['outcome_mismatch']}  "
        f"错误类别不同: {s['error_mismatch']}  "
        f"结果值不同: {s['value_mismatch']}",
        f"去重后的差异签名数: {s['unique_mismatch_signatures']}",
        "",
        "覆盖率",
        "-" * 60,
    ]
    cov = report["coverage"]
    lines.append(f"生成策略命中: {cov['strategy_counts']}")
    lines.append(f"边界情形命中: {cov['edge_counts']}")
    lines.append(
        f"最大生成深度: {cov['max_tree_depth_generated']}  "
        f"最大报文字节: {cov['max_size_generated']}"
    )
    lines.append(f"实现 A 错误类别: {cov['error_categories_a']}")
    lines.append(f"实现 B 错误类别: {cov['error_categories_b']}")
    lines.append("")
    lines.append("覆盖率提示")
    lines.append("-" * 60)
    hints = report["hints"]
    lines.extend(f"* {h}" for h in hints) if hints else lines.append("* 无")
    lines.append("")
    lines.append("差异与最小反例")
    lines.append("-" * 60)
    if not report["mismatches"]:
        lines.append("未发现差异。")
    for m in report["mismatches"]:
        lines.append(
            f"[{m['id']}] {m['category_label']}（策略 {m['strategy']}）"
            f"  {m['original_len']}B -> {m['minimized_len']}B"
        )
        lines.append(f"  最小反例(hex): {m['minimized_hex']}")
        lines.append(f"  实现 A 输出: {json.dumps(m['output_a'], ensure_ascii=False)}")
        lines.append(f"  实现 B 输出: {json.dumps(m['output_b'], ensure_ascii=False)}")
        lines.append(f"  样本文件: {m['minimized_file']}（原始: {m['original_file']}）")
        lines.append("")
    return "\n".join(lines) + "\n"
