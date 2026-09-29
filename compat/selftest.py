"""框架自测。

1. 用参考实现（v1/v2）跑完整矩阵 —— 必须全部 PASS，且矩阵结论符合预期语义；
2. 用三个故意写错的实现跑矩阵 —— 框架必须把它们判 FAIL，
   且失败类型符合预期（部分内容 / 内容不符 / 拒绝原因不符）。
任何一条不满足，自测失败并以退出码 1 结束。
"""

import sys
import tempfile

from . import format_v1, format_v2, samples
from .buggy import BUGGY_READERS
from .framework import (FAIL_CONTENT, FAIL_PARTIAL, REJECTED, run_matrix)

REFERENCE_READERS = {
    "v1 读取器": (1, format_v1.read_file),
    "v2 读取器": (2, format_v2.read_file),
}


def run(verbose=True):
    failures = []
    tmp = tempfile.mkdtemp(prefix="efmt-selftest-")
    samples.build_samples(tmp)
    cases = samples.load_cases(tmp)

    # 1. 参考实现：矩阵必须全 PASS
    cells = run_matrix(REFERENCE_READERS, cases)
    for c in cells:
        if c.verdict != "PASS":
            failures.append("参考实现未通过 [%s x %s]：%s"
                            % (c.case.name, c.reader_name, c.rationale))
    if verbose:
        print("[1] 参考实现矩阵：%d 格，%s"
              % (len(cells), "全部 PASS" if not failures else "存在 FAIL"))

    # 2. 错误实现：框架必须判失败，且失败类型正确
    checks = {
        "错误实现A·篡改后返回部分内容": (
            lambda case: case.scenario == "tampered", FAIL_PARTIAL),
        "错误实现B·跳过完整性校验": (
            lambda case: case.scenario == "tampered", FAIL_CONTENT),
        "错误实现C·不检查版本号": (
            lambda case: case.scenario == "normal"
            and case.file_version == 2, None),
    }
    for name, (version, fn) in BUGGY_READERS.items():
        bcells = run_matrix({name: (version, fn)}, cases)
        bad = [c for c in bcells if c.verdict == "FAIL"]
        if not bad:
            failures.append("框架未能识别错误实现：%s（全部判 PASS）" % name)
            continue
        pred, expected_kind = checks[name]
        target = [c for c in bcells if pred(c.case)]
        if not target or any(c.verdict != "FAIL" for c in target):
            failures.append("错误实现 %s 在关键用例上未被判失败" % name)
        elif expected_kind is None:
            # 错误C 把 v2 文件当 v1 解析：要么返回垃圾内容（fail_content），
            # 要么以错误原因拒绝（期望 UNSUPPORTED_VERSION，实际 INTEGRITY），
            # 两种形态都必须被判 FAIL，且不能是"碰巧按期望原因拒绝"。
            c = target[0]
            if c.actual[0] == REJECTED and c.actual[1] == c.expected[1]:
                failures.append("错误实现 %s 的失败类型不符预期：%s"
                                % (name, c.rationale))
        elif any(c.actual[0] != expected_kind for c in target):
            failures.append("错误实现 %s 的失败类型不符预期：期望 %s，实际 %s"
                            % (name, expected_kind,
                               [c.actual[0] for c in target]))
        if verbose:
            print("[2] %s：%d/%d 格被判 FAIL（关键用例：%s）"
                  % (name, len(bad), len(bcells),
                     target[0].rationale if target else "无"))
    return failures


def main():
    failures = run(verbose=True)
    if failures:
        print("\n自测失败：")
        for f in failures:
            print("  - " + f)
        return 1
    print("\n自测通过：参考实现矩阵全部符合预期；3 个错误实现均被框架判失败。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
