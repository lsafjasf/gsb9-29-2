"""兼容性测试执行器：不依赖具体加密实现，只依赖适配器协议。

适配器协议
==========
用例（Case）自带两个可调用对象：
- build_blob() -> bytes：生产者（某版本写入器）构造一个文件；
- reader：读取方（某版本读取器），需提供 .read(blob)。

read() 返回结果需含 plaintext(bytes) 与 needs_conversion(bool)。

read() 允许抛出“明确拒绝”异常，约定：
- 异常必须带 .reason 字符串（本框架的 EncFormatError 即满足）；
- 若异常还携带 partial_plaintext 属性，说明读取方暴露了部分明文，
  框架将判为 PARTIAL（永不通过）；
- 任何其他异常视为框架/实现缺陷 => ERROR。
"""

import dataclasses
import hashlib
from typing import Callable, Dict, List, Optional

from .errors import EncFormatError
from .verdicts import Verdict, REJECT_REASONS


@dataclasses.dataclass
class Case:
    """一个跨版本测试用例（矩阵中的一格）。"""

    scenario: str                 # 场景名，如 baseline / block_size_change ...
    producer: str                 # 生产者标签，如 "W1"
    reader: str                   # 读取方标签，如 "R2"
    expected: Verdict             # 期望判定
    build_blob: Callable[[], bytes]
    reader_impl: object
    expected_plaintext: bytes
    expected_reason: str = None   # 期望 REJECT 时的原因码（None 表示不校验原因）
    note: str = ""


@dataclasses.dataclass
class Cell:
    case: Case
    verdict: Verdict
    matched: bool
    evidence: str
    detail: Dict[str, object] = dataclasses.field(default_factory=dict)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def run_case(case: Case) -> Cell:
    """执行单个用例，依据“三值判据”给出结论与依据。

    判定规则：
    - 无异常返回：明文长度与 SHA256 必须与源完全一致，否则 FAIL；
      一致时 needs_conversion=True => CONVERT，否则 EQUIV。
    - 部分读取（异常携带 partial_plaintext）=> PARTIAL（永不通过）。
    - EncFormatError => REJECT，并记录原因码；与期望不符则 matched=False。
    - 其他异常 => ERROR（实现/框架缺陷）。
    """
    expected_pt = case.expected_plaintext
    try:
        blob = case.build_blob()
    except EncFormatError as exc:
        return _reject_cell(case, exc, produced=False)
    except Exception as exc:  # 生成阶段崩溃属于框架/实现缺陷
        return Cell(
            case, Verdict.ERROR, False,
            f"生产者构造文件时抛出未预期异常 {type(exc).__name__}: {exc}",
            {"exception": type(exc).__name__},
        )

    try:
        result = case.reader_impl.read(blob)
    except EncFormatError as exc:
        partial = getattr(exc, "partial_plaintext", None)
        if partial is not None:
            return Cell(
                case, Verdict.PARTIAL, False,
                f"读取方暴露了 {len(partial)} 字节部分明文后报错："
                f"{type(exc).__name__}({exc.reason})；读到部分内容不算通过",
                {"reason": exc.reason, "partial_len": len(partial)},
            )
        return _reject_cell(case, exc, produced=True)
    except Exception as exc:
        return Cell(
            case, Verdict.ERROR, False,
            f"读取方抛出未预期异常 {type(exc).__name__}: {exc}",
            {"exception": type(exc).__name__},
        )

    plaintext = getattr(result, "plaintext", None)
    if plaintext is None:
        return Cell(case, Verdict.ERROR, False,
                    "读取结果缺少 plaintext 属性", {})

    ok_len = len(plaintext) == len(expected_pt)
    ok_hash = hashlib.sha256(plaintext).digest() == hashlib.sha256(expected_pt).digest()
    if not (ok_len and ok_hash):
        return Cell(
            case, Verdict.FAIL, False,
            f"解密返回但内容不等价：长度{'一致' if ok_len else '不一致'}"
            f"(得到 {len(plaintext)}/期望 {len(expected_pt)})，"
            f"SHA256{'一致' if ok_hash else '不一致'}"
            f"(得到 {sha256_hex(plaintext)}/期望 {sha256_hex(expected_pt)})",
            {"got_len": len(plaintext), "want_len": len(expected_pt),
             "got_sha": sha256_hex(plaintext), "want_sha": sha256_hex(expected_pt)},
        )

    needs_conversion = bool(getattr(result, "needs_conversion", False))
    verdict = Verdict.CONVERT if needs_conversion else Verdict.EQUIV
    matched = verdict == case.expected
    if matched:
        if verdict == Verdict.EQUIV:
            ev = (f"明文逐字节等价：长度 {len(plaintext)}，"
                  f"SHA256={sha256_hex(plaintext)}…；读取方原生支持"
                  f" v{getattr(result, 'version', '?')}，无需转换")
        else:
            ev = (f"明文逐字节等价：长度 {len(plaintext)}，"
                  f"SHA256={sha256_hex(plaintext)}…；文件为旧版 "
                  f"v{getattr(result, 'version', '?')}，读取方报告 "
                  f"needs_conversion=True，结论：可读但需转换")
    else:
        ev = f"行为得到 {verdict.value}，但期望 {case.expected.value}"
    detail = {
        "got_len": len(plaintext),
        "sha256": sha256_hex(plaintext),
        "file_version": getattr(result, "version", None),
        "needs_conversion": needs_conversion,
        "block_size": getattr(result, "block_size", None),
        "block_count": getattr(result, "block_count", None),
    }
    return Cell(case, verdict, matched, ev, detail)


def _reject_cell(case: Case, exc: EncFormatError, *, produced: bool) -> Cell:
    reason = getattr(exc, "reason", "enc_format_error")
    reason_text = REJECT_REASONS.get(reason, reason)
    stage = "读取" if produced else "构造"
    matched = case.expected == Verdict.REJECT and (
        case.expected_reason is None or case.expected_reason == reason
    )
    ev = (f"明确拒绝：{stage}阶段抛出 {type(exc).__name__}，"
          f"原因码 reason={reason}（{reason_text}）；未返回部分明文")
    if case.expected != Verdict.REJECT:
        ev += f"；但期望为 {case.expected.value}"
    elif case.expected_reason is not None and case.expected_reason != reason:
        ev += f"；但拒绝原因不匹配，期望 reason={case.expected_reason}"
    return Cell(case, Verdict.REJECT, matched, ev, {"reason": reason})


def run_matrix(cases: List[Case]) -> List[Cell]:
    return [run_case(case) for case in cases]


def summarize(cells: List[Cell]) -> Dict[str, int]:
    summary = {v.value: 0 for v in Verdict}
    summary["unexpected"] = 0
    for cell in cells:
        summary[cell.verdict.value] += 1
        if not cell.matched:
            summary["unexpected"] += 1
    summary["total"] = len(cells)
    return summary
