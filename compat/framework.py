"""兼容性判定框架。

判定模型
--------
每个测试单元（矩阵的一格）= 一个读取器 x 一个测试用例。
读取器只能有两种行为：返回 ReadOk，或抛出 RejectError(reason)。

实际结果分类：
    ok_equal       读取成功，明文与期望逐字节等价，且未做格式转换
    ok_converted   读取成功，明文等价，但实现报告做了格式转换（如块重切分）
    rejected:<R>   明确拒绝，原因码 R
    fail_partial   返回了部分内容（是期望明文的前缀但更短）——不算通过
    fail_content   返回了与期望不符的内容
    fail_crash     抛出非 RejectError 异常（崩溃）

通过判据（实际结果必须与用例期望完全一致）：
    期望 ok_equal       -> 只有 ok_equal 通过
    期望 ok_converted   -> 只有 ok_converted 通过（没报告转换也算失败，
                           因为框架无法区分"真的等价"与"碰巧等价"）
    期望 rejected:<R>   -> 必须拒绝且原因码一致（原因不符同样失败：
                           例如把"版本不支持"误报成"完整性错误"会误导运维）
"""

from dataclasses import dataclass

from .errors import Reason, RejectError

OK_EQUAL = "ok_equal"
OK_CONVERTED = "ok_converted"
REJECTED = "rejected"
FAIL_PARTIAL = "fail_partial"
FAIL_CONTENT = "fail_content"
FAIL_CRASH = "fail_crash"


@dataclass
class ReadOk:
    """读取器成功读取文件时的返回值。"""
    plaintext: bytes
    converted: bool = False   # 是否做了格式转换（如旧版文件按新版块布局重切分）
    note: str = ""


@dataclass
class Case:
    """一个测试用例（一份输入文件 + 对每个读取器版本的期望结果）。"""
    name: str
    group: str                       # 矩阵中的分组（行块）
    blob: bytes                      # 输入文件内容
    keyring: dict                    # 提供给读取器的密钥环 {kv: key}
    file_version: int                # 文件格式版本
    scenario: str                    # normal / tampered / missingkey / roundtrip
    expected_plaintext: bytes        # 期望解密出的明文
    basis: str = ""                  # 该用例期望结果的依据说明


def expected_outcome(reader_version, case):
    """根据读取器版本与用例场景，给出期望结果。

    参考语义：读取器向后兼容（新版读旧版需转换），不向前兼容
    （旧版读新版必须明确拒绝 UNSUPPORTED_VERSION）。
    注意检查顺序：版本门在完整性/密钥检查之前——旧版读取器无法解析
    新版文件头，因此对新版文件（即使被篡改或缺密钥）的期望拒绝原因
    是 UNSUPPORTED_VERSION，而不是 INTEGRITY/KEY_UNAVAILABLE。
    """
    if reader_version == case.file_version:
        if case.scenario == "tampered":
            return (REJECTED, Reason.INTEGRITY)
        if case.scenario == "missingkey":
            return (REJECTED, Reason.KEY_UNAVAILABLE)
        return (OK_EQUAL,)
    if reader_version > case.file_version:
        if case.scenario == "tampered":
            return (REJECTED, Reason.INTEGRITY)
        if case.scenario == "missingkey":
            return (REJECTED, Reason.KEY_UNAVAILABLE)
        return (OK_CONVERTED,)
    return (REJECTED, Reason.UNSUPPORTED_VERSION)


@dataclass
class Cell:
    case: Case
    reader_name: str
    expected: tuple
    actual: tuple
    verdict: str        # "PASS" / "FAIL"
    rationale: str


def _classify_ok(res, case):
    exp = case.expected_plaintext
    got = res.plaintext
    if got == exp:
        return (OK_CONVERTED,) if res.converted else (OK_EQUAL,)
    if exp.startswith(got) and len(got) < len(exp):
        return (FAIL_PARTIAL, len(got), len(exp))
    diff = next((i for i, (a, b) in enumerate(zip(got, exp)) if a != b),
                min(len(got), len(exp)))
    return (FAIL_CONTENT, diff, len(got), len(exp))


def run_cell(reader_name, reader_version, reader_fn, case):
    expected = expected_outcome(reader_version, case)
    try:
        res = reader_fn(case.blob, case.keyring)
    except RejectError as e:
        actual = (REJECTED, e.reason)
        actual_detail = e.detail
    except Exception as e:  # noqa: BLE001 - 崩溃必须被捕获并判失败
        actual = (FAIL_CRASH, "%s: %s" % (type(e).__name__, e))
        actual_detail = ""
    else:
        if not isinstance(res, ReadOk):
            actual = (FAIL_CRASH, "返回值不是 ReadOk: %r" % (res,))
            actual_detail = ""
        else:
            actual = _classify_ok(res, case)
            actual_detail = res.note

    kind = actual[0]
    if kind in (FAIL_PARTIAL, FAIL_CONTENT, FAIL_CRASH):
        if kind == FAIL_PARTIAL:
            rationale = ("仅返回部分内容（%d/%d 字节），读到部分内容不算通过"
                         % (actual[1], actual[2]))
        elif kind == FAIL_CONTENT:
            rationale = ("返回内容与期望明文不符（首处差异偏移 %d，长度 %d/%d）"
                         % (actual[1], actual[2], actual[3]))
        else:
            rationale = "读取器崩溃或契约违规：%s" % actual[1]
        return Cell(case, reader_name, expected, actual, "FAIL", rationale)

    if kind == REJECTED:
        if expected[0] == REJECTED and expected[1] == actual[1]:
            verdict, rationale = "PASS", "按预期明确拒绝：%s（%s）" % (actual[1], actual_detail)
        elif expected[0] == REJECTED:
            verdict = "FAIL"
            rationale = "拒绝原因不符：期望 %s，实际 %s（%s）" % (expected[1], actual[1], actual_detail)
        else:
            verdict = "FAIL"
            rationale = "期望可读（%s），实际拒绝 %s（%s）" % (expected[0], actual[1], actual_detail)
        return Cell(case, reader_name, expected, actual, verdict, rationale)

    # ok_equal / ok_converted
    if expected[0] == REJECTED:
        verdict = "FAIL"
        rationale = "应明确拒绝（%s）却读取成功" % expected[1]
    elif expected[0] == kind:
        if kind == OK_EQUAL:
            verdict, rationale = "PASS", "可读且内容逐字节等价"
        else:
            verdict, rationale = "PASS", "可读但需转换：%s" % (actual_detail or "实现报告已做格式转换")
    else:
        verdict = "FAIL"
        rationale = "读取方式与期望不符：期望 %s，实际 %s" % (expected[0], kind)
    return Cell(case, reader_name, expected, actual, verdict, rationale)


def run_matrix(readers, cases):
    """readers: {名称: (版本号, read_fn)}；返回所有 Cell。"""
    cells = []
    for case in cases:
        for reader_name, (reader_version, reader_fn) in readers.items():
            cells.append(run_cell(reader_name, reader_version, reader_fn, case))
    return cells


_CELL_LABEL = {
    OK_EQUAL: "可读·等价",
    OK_CONVERTED: "可读·需转换",
    FAIL_PARTIAL: "部分内容",
    FAIL_CONTENT: "内容不符",
    FAIL_CRASH: "崩溃",
}


def _cell_text(cell):
    if cell.actual[0] == REJECTED:
        body = "拒绝·%s" % cell.actual[1]
    else:
        body = _CELL_LABEL.get(cell.actual[0], cell.actual[0])
    return ("✅ " if cell.verdict == "PASS" else "❌ ") + body


def render_markdown(cells, reader_names):
    """渲染兼容矩阵（Markdown）：总表 + 每格结论与依据。"""
    lines = []
    lines.append("# 加密文件格式跨版本兼容矩阵")
    lines.append("")
    lines.append("图例：✅=符合预期（通过） ❌=不符合预期（失败）；"
                 "“可读·等价”=内容逐字节相同；“可读·需转换”=内容等价但实现做了格式转换；"
                 "“拒绝·X”=以原因码 X 明确拒绝。")
    lines.append("")
    groups = []
    for c in cells:
        if c.case.group not in groups:
            groups.append(c.case.group)
    for group in groups:
        lines.append("## %s" % group)
        lines.append("")
        header = "| 用例 |" + "|".join(" %s " % r for r in reader_names) + "|"
        lines.append(header)
        lines.append("|" + "---|" * (len(reader_names) + 1))
        group_cases = []
        for c in cells:
            if c.case.group == group and c.case not in group_cases:
                group_cases.append(c.case)
        for case in group_cases:
            row = ["| " + case.name + " "]
            for r in reader_names:
                cell = next(c for c in cells if c.case is case and c.reader_name == r)
                row.append("| " + _cell_text(cell) + " ")
            lines.append("".join(row) + "|")
        lines.append("")
    lines.append("## 每格结论与依据")
    lines.append("")
    lines.append("| 用例 | 读取器 | 结论 | 依据 |")
    lines.append("|---|---|---|---|")
    for c in cells:
        lines.append("| %s | %s | %s | %s |"
                     % (c.case.name, c.reader_name, c.verdict,
                        c.rationale.replace("|", "\\|")))
    lines.append("")
    return "\n".join(lines)

