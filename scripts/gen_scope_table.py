#!/usr/bin/env python3
"""根据 tests/scope_cases.py 生成作用域预期/实际对照表 (docs/SCOPE_CASES.md)。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from cookiekit import CookieJar, RejectCode  # noqa: E402
from scope_cases import CASES  # noqa: E402

FIXED_NOW = 1700000000.0


def actual(case):
    jar = CookieJar(clock=lambda: FIXED_NOW)
    result = jar.set_cookie(case.header, case.set_url)
    if not result.accepted:
        return f"拒绝（{result.code.value}）"
    if case.request_url is None:
        return "接受"
    header = jar.cookie_header(case.request_url)
    name = case.header.split(";", 1)[0].split("=", 1)[0].strip()
    sent = bool(header) and name in header
    return "接受，回传" if sent else "接受，不回传"


def expected(case):
    if not case.expect_accept:
        return f"拒绝（{case.expect_code}）"
    if case.request_url is None:
        return "接受"
    return "接受，回传" if case.expect_sent else "接受，不回传"


def render():
    lines = [
        "# 作用域用例集：预期与实际对照表",
        "",
        "- 参照基线：RFC 6265 §4/§5（Cookie 作用域）与 §5.4（回传选择）",
        "- 时钟固定注入为 `1700000000.0`；公共后缀清单使用库内置示例集（`com`、`co.uk`、`appspot.com` 等，可注入替换）",
        "- “实际”一列由 `scripts/gen_scope_table.py` 真实执行 CookieJar 得出；`tests/test_scope.py` 对每行做相等断言",
        "",
        "| 编号 | 场景 | Set-Cookie | 设置 URL | 请求 URL | 预期 | 实际 | 一致 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in CASES:
        exp, act = expected(case), actual(case)
        match = "是" if exp == act else "**否**"
        lines.append(
            f"| {case.cid} | {case.desc} | `{case.header}` | `{case.set_url}` "
            f"| `{case.request_url or '—'}` | {exp} | {act} | {match} |"
        )
    lines += [
        "",
        "## 说明",
        "",
        "- **SC-01 / SC-02**：无 `Domain` 属性时为 host-only，仅设置主机本身可见。",
        "- **SC-03 ~ SC-05**：`Domain` 只能上溯到父域，不能下钻到子域。",
        "- **SC-06 ~ SC-08**：子域匹配必须以 `.` 为边界，禁止前缀包含。",
        "- **SC-11 ~ SC-13 / SC-27 / SC-28**：`Domain` 为公共后缀时拒绝；主机本身恰为该后缀时按 host-only 接受（RFC 6265 §5.3 第 6 步）。",
        "- **SC-14 ~ SC-17 / SC-26**：IP 字面量只做精确匹配，不允许任何后缀域。",
        "- **SC-18**：Cookie 不按端口隔离（RFC 6265 §7.2），是否收紧由调用方决定。",
        "- **SC-19 ~ SC-25**：路径匹配要求路径边界为 `/`，默认路径取请求 URI 最后一段 `/` 之前的部分。",
        "- **SC-29 / SC-30**：空 `Domain` 回退 host-only；主机尾点规范化。",
        "",
    ]
    return "\n".join(lines)


def main():
    out = os.path.join(ROOT, "docs", "SCOPE_CASES.md")
    content = render()
    with open(out, "w", encoding="utf-8") as f:
        f.write(content)
    print(content)


if __name__ == "__main__":
    main()
