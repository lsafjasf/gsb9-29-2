"""兼容矩阵报告渲染：终端表格 / Markdown / JSON。"""

import json
from typing import List

from .harness import Cell, summarize
from .matrix import ConversionResult
from .verdicts import Verdict

# 中文终端宽度对齐（结论用 ASCII 标记，依据内联展示）
VERDICT_MARK = {
    Verdict.EQUIV: "EQUIV  ",
    Verdict.CONVERT: "CONVERT",
    Verdict.REJECT: "REJECT ",
    Verdict.PARTIAL: "PARTIAL",
    Verdict.FAIL: "FAIL   ",
    Verdict.ERROR: "ERROR  ",
    Verdict.NA: "NA     ",
}

SCENARIO_TITLES = {
    "baseline": "基线跨版本读写",
    "bigblock": "块大小变化：声明变大(1024->2048)",
    "smallblock": "块大小变化：声明变小(1024->512)",
    "key_missing": "密钥版本缺失",
    "block_tampered": "数据块被篡改",
    "truncated": "文件截断(丢末块)",
}


def _scenario(cell: Cell) -> str:
    return cell.case.scenario


def render_text(cells: List[Cell], conversion: ConversionResult) -> str:
    lines = []
    lines.append("加密文件格式跨版本兼容矩阵")
    lines.append("=" * 78)
    header = f"{'场景':<34}{'生产者':<8}{'读取方':<8}{'结论':<9}{'符合期望'}"
    lines.append(header)
    lines.append("-" * 78)
    for cell in cells:
        title = SCENARIO_TITLES.get(cell.case.scenario, cell.case.scenario)
        if len(title) > 32:
            title = title[:31] + "…"
        match = "是" if cell.matched else "否 <<<"
        lines.append(
            f"{title:<34}{cell.case.producer:<8}{cell.case.reader:<8}"
            f"{VERDICT_MARK[cell.verdict]:<9}{match}"
        )
    lines.append("-" * 78)
    summary = summarize(cells)
    lines.append(
        "统计：" + " ".join(
            f"{v.value}={summary[v.value]}" for v in Verdict
        ) + f"  不符合期望={summary['unexpected']}"
    )
    lines.append("")
    lines.append("转换路径检查 (CONVERT 可执行性)：" +
                 ("通过" if conversion.ok else "失败 <<<"))
    lines.append("  " + conversion.evidence)
    lines.append("")
    lines.append("各格依据：")
    for cell in cells:
        title = SCENARIO_TITLES.get(cell.case.scenario, cell.case.scenario)
        flag = "" if cell.matched else "  [期望不符]"
        lines.append(f"- [{title}] {cell.case.producer}→{cell.case.reader}: "
                     f"{cell.verdict.value}{flag}")
        lines.append(f"    {cell.evidence}")
    return "\n".join(lines)


def render_markdown(cells: List[Cell], conversion: ConversionResult) -> str:
    lines = [
        "# 加密文件格式跨版本兼容矩阵",
        "",
        "判定：`EQUIV`=可读且内容逐字节等价；`CONVERT`=可读但需转换；"
        "`REJECT`=明确拒绝（附原因码）。",
        "`PARTIAL`/`FAIL`/`ERROR` 均为不通过。",
        "",
        "| 场景 | 生产者 | 读取方 | 结论 | 拒绝原因 | 符合期望 | 依据 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cell in cells:
        reason = cell.detail.get("reason", "") if cell.verdict == Verdict.REJECT else ""
        lines.append(
            f"| {SCENARIO_TITLES.get(cell.case.scenario, cell.case.scenario)} "
            f"| {cell.case.producer} | {cell.case.reader} "
            f"| **{cell.verdict.value}** | {reason} "
            f"| {'是' if cell.matched else '否'} | {cell.evidence} |"
        )
    lines += [
        "",
        "## 转换路径检查",
        "",
        f"结果：**{'通过' if conversion.ok else '失败'}**",
        "",
        conversion.evidence,
        "",
        f"统计：{'  '.join(f'{k}={v}' for k, v in summarize(cells).items())}",
        "",
    ]
    return "\n".join(lines)


def render_json(cells: List[Cell], conversion: ConversionResult) -> str:
    payload = {
        "cells": [
            {
                "scenario": c.case.scenario,
                "producer": c.case.producer,
                "reader": c.case.reader,
                "expected": c.case.expected.value,
                "expected_reason": c.case.expected_reason,
                "verdict": c.verdict.value,
                "matched": c.matched,
                "evidence": c.evidence,
                "detail": c.detail,
            }
            for c in cells
        ],
        "conversion_check": dataclasses_asdict(conversion),
        "summary": summarize(cells),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def dataclasses_asdict(obj):
    import dataclasses
    return dataclasses.asdict(obj)
