#!/usr/bin/env python3
"""secret_sharing 库的自检测试。

覆盖：
* GF(2^8) 运算正确性（与朴素实现全量对比）
* 门限组合枚举：任意 t 份可精确恢复，任意 t-1 份被拒绝
* 边界：阈值 1、阈值等于份数、份额不足、超长秘密、空秘密、参数越界
* 篡改检出：载荷/下标/摘要/阈值被改、混入其他方案份额、重复份额
* 份额序列化往返与非法格式

运行：python3 selftest.py
输出：终端测试报告 + combination_verification.json（组合验证数据）
退出码：全部通过为 0，任一失败为 1。
"""

import itertools
import json
import os
import sys
import time

import secret_sharing as ss

PASS = 0
FAIL = 0
FAILURES = []


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        FAILURES.append((name, repr(exc)))
        print(f"  [FAIL] {name}: {exc!r}")
    else:
        PASS += 1
        print(f"  [ok]   {name}")


def expect_raises(exc_types, fn):
    try:
        fn()
    except exc_types:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"抛出了非预期异常 {exc!r}") from exc
    raise AssertionError(f"未抛出预期异常 {exc_types}")


# ---------------------------------------------------------------------------
# 1. GF(2^8) 运算
# ---------------------------------------------------------------------------


def _mul_naive(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
    return p


def test_gf():
    for a in range(256):
        for b in range(256):
            assert ss._gf_mul(a, b) == _mul_naive(a, b), (a, b)
    for a in range(1, 256):
        assert ss._gf_mul(a, ss._gf_inv(a)) == 1
    # 分配律抽查
    for a in (0, 1, 2, 3, 0x53, 0xCA, 0xFF):
        for b in range(0, 256, 17):
            for c in range(0, 256, 31):
                lhs = ss._gf_mul(a, b ^ c)
                rhs = ss._gf_mul(a, b) ^ ss._gf_mul(a, c)
                assert lhs == rhs, (a, b, c)


# ---------------------------------------------------------------------------
# 2. 组合枚举验证（同时产出验证数据）
# ---------------------------------------------------------------------------

COMBO_SCHEMES = [(2, 3), (3, 5), (4, 7), (5, 8)]


def run_combination_enumeration():
    """对每个 (t, n) 枚举全部 C(n,t) 组合恢复，全部 t-1 子集必须被拒绝。"""
    report_cases = []
    for t, n in COMBO_SCHEMES:
        secret = os.urandom(32)
        shares = ss.split_secret(secret, t, n)
        case = {
            "scheme": f"{t}-of-{n}",
            "threshold": t,
            "share_count": n,
            "secret_hex": secret.hex(),
            "share_indexes": [s.index for s in shares],
            "exact_threshold_combinations": [],
            "insufficient_combinations": [],
            "superset_combinations_checked": 0,
        }
        # 全部恰好 t 份的组合：必须精确恢复
        for combo in itertools.combinations(range(n), t):
            picked = [shares[i] for i in combo]
            recovered = ss.recover_secret(picked)
            ok = recovered == secret
            assert ok, f"{t}-of-{n} 组合 {combo} 恢复结果不一致"
            case["exact_threshold_combinations"].append(
                {"indexes": [shares[i].index for i in combo], "recovered_ok": ok}
            )
        # 全部 t-1 份的组合：必须报份额不足，且绝不返回秘密
        for combo in itertools.combinations(range(n), t - 1):
            picked = [shares[i] for i in combo]
            expect_raises(ss.InsufficientSharesError, lambda p=picked: ss.recover_secret(p))
            case["insufficient_combinations"].append(
                {"indexes": [shares[i].index for i in combo], "result": "rejected:InsufficientSharesError"}
            )
        # 超过 t 份的组合（抽查：全部 t+1 组合 + 全集）
        for combo in itertools.combinations(range(n), t + 1):
            picked = [shares[i] for i in combo]
            assert ss.recover_secret(picked) == secret
            case["superset_combinations_checked"] += 1
        assert ss.recover_secret(shares) == secret
        case["superset_combinations_checked"] += 1
        report_cases.append(case)
        total = len(case["exact_threshold_combinations"])
        print(f"  [ok]   {t}-of-{n}: {total} 个阈值组合全部精确恢复，"
              f"{len(case['insufficient_combinations'])} 个不足组合全部被拒绝，"
              f"{case['superset_combinations_checked']} 个超阈值组合全部精确恢复")
    return report_cases


# ---------------------------------------------------------------------------
# 3. 边界情形
# ---------------------------------------------------------------------------


def test_threshold_1():
    secret = b"threshold-one-backup"
    shares = ss.split_secret(secret, 1, 4)
    for s in shares:  # 任意单独一份即可恢复
        assert ss.recover_secret([s]) == secret
    assert ss.recover_secret(shares) == secret  # 多份一致性也通过


def test_threshold_equals_n():
    secret = os.urandom(64)
    shares = ss.split_secret(secret, 5, 5)
    assert ss.recover_secret(shares) == secret
    for drop in range(5):  # 少任何一份都不行
        subset = shares[:drop] + shares[drop + 1:]
        expect_raises(ss.InsufficientSharesError, lambda p=subset: ss.recover_secret(p))


def test_insufficient_shares():
    secret = os.urandom(16)
    shares = ss.split_secret(secret, 3, 6)
    expect_raises(ss.InsufficientSharesError, lambda: ss.recover_secret([]))
    expect_raises(ss.InsufficientSharesError, lambda: ss.recover_secret(shares[:1]))
    expect_raises(ss.InsufficientSharesError, lambda: ss.recover_secret(shares[:2]))


def test_long_secret():
    secret = os.urandom(100 * 1024)  # 100 KB 超长秘密
    shares = ss.split_secret(secret, 3, 5)
    t0 = time.perf_counter()
    for combo in itertools.combinations(range(5), 3):
        assert ss.recover_secret([shares[i] for i in combo]) == secret
    elapsed = time.perf_counter() - t0
    print(f"         (100KB 秘密，10 个组合全部精确恢复，耗时 {elapsed:.2f}s)")


def test_empty_secret():
    shares = ss.split_secret(b"", 2, 3)
    assert ss.recover_secret(shares[:2]) == b""


def test_parameter_validation():
    secret = b"x"
    expect_raises(ss.InvalidParameterError, lambda: ss.split_secret(secret, 0, 3))
    expect_raises(ss.InvalidParameterError, lambda: ss.split_secret(secret, 4, 3))
    expect_raises(ss.InvalidParameterError, lambda: ss.split_secret(secret, 1, 256))
    expect_raises(ss.InvalidParameterError, lambda: ss.split_secret(secret, -1, 3))
    expect_raises(ss.InvalidParameterError, lambda: ss.split_secret("not-bytes", 2, 3))
    # 最大份数 255 可用
    shares = ss.split_secret(secret, 2, ss.MAX_SHARES)
    assert len(shares) == 255
    assert ss.recover_secret([shares[0], shares[254]]) == secret
    # 1-of-1
    one = ss.split_secret(secret, 1, 1)
    assert ss.recover_secret(one) == secret


# ---------------------------------------------------------------------------
# 4. 篡改检出
# ---------------------------------------------------------------------------


def _tamper_payload(share, pos=0):
    payload = bytearray(share.payload)
    payload[pos % len(payload)] ^= 0x01
    return ss.Share(share.index, share.threshold, share.share_count, share.digest, bytes(payload))


def run_tamper_tests():
    results = []
    secret = os.urandom(32)
    shares = ss.split_secret(secret, 3, 5)

    def record(name, fn, expected):
        try:
            fn()
        except expected as exc:
            results.append({"case": name, "detected": True, "exception": type(exc).__name__})
            print(f"  [ok]   篡改检出 - {name} -> {type(exc).__name__}")
            return
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{name}: 抛出非预期异常 {exc!r}") from exc
        raise AssertionError(f"{name}: 篡改未被检出！")

    # 4.1 篡改份额载荷（恢复集合内）
    record(
        "载荷字节被翻转（在恢复集合内）",
        lambda: ss.recover_secret([_tamper_payload(shares[0]), shares[1], shares[2]]),
        ss.TamperedShareError,
    )
    # 4.2 篡改多余的第 4 份（份额间一致性检查应命中）
    record(
        "多余份额被篡改（一致性检查）",
        lambda: ss.recover_secret([shares[0], shares[1], shares[2], _tamper_payload(shares[3])]),
        ss.TamperedShareError,
    )
    # 4.3 篡改内嵌摘要
    bad_digest = ss.Share(shares[0].index, 3, 5, b"\x00" * 32, shares[0].payload)
    record(
        "内嵌 SHA-256 摘要被改",
        lambda: ss.recover_secret([bad_digest, shares[1], shares[2]]),
        ss.TamperedShareError,
    )
    # 4.4 重复份额
    record(
        "混入重复份额",
        lambda: ss.recover_secret([shares[0], shares[0], shares[1]]),
        ss.TamperedShareError,
    )
    # 4.5 混入另一方案的份额
    other = ss.split_secret(secret, 3, 5)
    record(
        "混入其他拆分批次的份额",
        lambda: ss.recover_secret([shares[0], shares[1], other[2]]),
        ss.TamperedShareError,
    )
    # 4.6 篡改份额下标
    bad_index = ss.Share(9, 3, 5, shares[0].digest, shares[0].payload)
    record(
        "份额下标被改",
        lambda: ss.recover_secret([bad_index, shares[1], shares[2]]),
        ss.TamperedShareError,
    )
    # 4.7 阈值被改小（t=3 -> 2，试图用 2 份恢复）
    forged = [ss.Share(s.index, 2, 5, s.digest, s.payload) for s in shares[:2]]
    record(
        "阈值字段被改小（伪造 2-of-5）",
        lambda: ss.recover_secret(forged),
        (ss.TamperedShareError, ss.InsufficientSharesError),
    )
    # 4.8 阈值被改大（t=3 -> 5，只给 3 份）
    forged5 = [ss.Share(s.index, 5, 5, s.digest, s.payload) for s in shares[:3]]
    record(
        "阈值字段被改大（份额不足）",
        lambda: ss.recover_secret(forged5),
        (ss.TamperedShareError, ss.InsufficientSharesError),
    )
    # 4.9 阈值 1 时篡改载荷
    one_shares = ss.split_secret(secret, 1, 3)
    record(
        "阈值 1 时载荷被篡改",
        lambda: ss.recover_secret([_tamper_payload(one_shares[0])]),
        ss.TamperedShareError,
    )
    return results


# ---------------------------------------------------------------------------
# 5. 序列化
# ---------------------------------------------------------------------------


def test_serialization():
    secret = os.urandom(48)
    shares = ss.split_secret(secret, 3, 5)
    texts = [s.serialize() for s in shares]
    parsed = [ss.Share.parse(t) for t in texts]
    assert parsed == shares
    assert ss.recover_secret(parsed[:3]) == secret
    # 直接传字符串也可以恢复
    assert ss.recover_secret(texts[:3]) == secret
    # 非法格式
    expect_raises(ss.InvalidShareError, lambda: ss.Share.parse("hello"))
    expect_raises(ss.InvalidShareError, lambda: ss.Share.parse("v1:1:2:3:@@@:AAAA"))
    expect_raises(ss.InvalidShareError, lambda: ss.Share.parse("v1:0:2:3:QUJD:QUJD"))  # 下标 0
    expect_raises(ss.InvalidShareError, lambda: ss.Share.parse("v2:1:2:3:QUJD:QUJD"))  # 版本错
    expect_raises(ss.InvalidShareError, lambda: ss.recover_secret([12345]))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    print("== GF(2^8) 有限域运算 ==")
    check("乘法表与朴素实现全量一致 + 逆元 + 分配律", test_gf)

    print("== 门限组合枚举（精确恢复 / 不足拒绝） ==")
    combo_cases = run_combination_enumeration()

    print("== 边界情形 ==")
    check("阈值 1：任意单份可恢复", test_threshold_1)
    check("阈值等于份数（5-of-5）：缺一不可", test_threshold_equals_n)
    check("份额不足：0/1/2 份均被拒绝", test_insufficient_shares)
    check("超长秘密：100KB，3-of-5 全组合", test_long_secret)
    check("空秘密", test_empty_secret)
    check("参数校验：t=0 / t>n / n>255 / 非 bytes / 255 份上限 / 1-of-1", test_parameter_validation)

    print("== 篡改检出 ==")
    tamper_results = run_tamper_tests()

    print("== 份额序列化 ==")
    check("序列化往返 + 非法格式拒绝", test_serialization)

    report = {
        "scheme": "Shamir secret sharing over GF(2^8), modulus x^8+x^4+x^3+x+1 (0x11B), generator 3",
        "tamper_detection": "per-share SHA-256(secret) commitment + cross-share polynomial consistency",
        "combination_cases": combo_cases,
        "tamper_cases": tamper_results,
        "summary": {"passed": PASS, "failed": FAIL},
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "combination_verification.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\n组合验证数据已写入 {out}")
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
