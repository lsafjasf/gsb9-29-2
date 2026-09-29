"""跨版本兼容矩阵的用例构建与转换路径验证。

矩阵覆盖（行=场景×生产者，列=读取方版本）：

  baseline              W1 / W2   基线：版本内读写 + 新旧互读
  block_size_change     W1        块大小声明变化（变大/变小）
  key_missing           W1 / W2   读取方密钥环缺少文件声明的 key_version
  block_tampered        W1 / W2   数据块密文翻转 1 bit
  truncated             W1 / W2   文件被截断，丢失末块
  conversion_check      W1        v2 读 v1 后用 v2 重写，再验证往返等价

每格都有明确期望（判定 + REJECT 的原因码），框架实际行为与期望不符即失败。
"""

import dataclasses
import hashlib
from typing import Dict, List, Tuple

from .codec import V1, V2, MAGIC, Reader, Writer, KeyRing, MAC_LEN
from . import fixtures
from .harness import Case, Cell, run_case
from .verdicts import Verdict

READER_LABELS = {V1: "R1", V2: "R2"}
PRODUCER_LABELS = {V1: "W1", V2: "W2"}


@dataclasses.dataclass
class ConversionResult:
    """转换路径检查结果（CONVERT 不只是口头声明，必须能真正升级并往返）。"""

    ok: bool
    evidence: str
    detail: Dict[str, object] = dataclasses.field(default_factory=dict)


def _builders(pt):
    """返回各场景下两个版本文件的构造器（延迟执行，保证用例独立）。"""
    keys = fixtures.FIXED_KEYS

    def baseline(version):
        bs = {V1: 1024, V2: 2048}[version]
        nonce = b"\x00" * 16 if version == V1 else fixtures.FIXED_NONCE_V2
        return Writer(version, keys[version], bs, key_version=version).write(
            pt, nonce=nonce
        )

    return {
        "baseline": {V1: lambda: baseline(V1), V2: lambda: baseline(V2)},
        "key_missing": {
            V1: lambda: fixtures.load_scenario("key_missing", V1),
            V2: lambda: fixtures.load_scenario("key_missing", V2),
        },
        "block_tampered": {
            V1: lambda: fixtures.load_scenario("tampered", V1),
            V2: lambda: fixtures.load_scenario("tampered", V2),
        },
        "truncated": {
            V1: lambda: fixtures.load_scenario("truncated", V1),
            V2: lambda: fixtures.load_scenario("truncated", V2),
        },
        "bigblock": {V1: lambda: fixtures.load_scenario("bigblock", V1)},
        "smallblock": {V1: lambda: _rewrite_v1_blocksize(pt, 512)},
    }


def _rewrite_v1_blocksize(pt: bytes, declared_bs: int) -> bytes:
    """构造一个 v1 文件：保留真实 1024 字节分块，但头部声明另一个块大小。

    用于“块大小变小”场景：真实块长度超过声明值，读取方必须拒绝
    （block_too_large），不能静默截断。
    """
    import hmac
    from .codec import _HDR_V1, _derive
    blob = bytearray(
        Writer(V1, fixtures.FIXED_KEYS[1], 1024, key_version=1).write(pt)
    )
    magic, ver, kv, _, bc = _HDR_V1.unpack(bytes(blob[:20]))
    header = _HDR_V1.pack(MAGIC, V1, kv, declared_bs, bc)
    blob[:20] = header
    hdr_mac_key, _, _ = _derive(V1, kv, fixtures.FIXED_KEYS[kv])
    blob[20:20 + MAC_LEN] = hmac.new(
        hdr_mac_key, header, hashlib.sha256
    ).digest()
    return bytes(blob)


def _readers() -> Dict[Tuple[str, int, int], Reader]:
    """{(scenario, reader_version, file_version): Reader}，密钥环按场景变化。"""
    full = fixtures.combined_ring()
    only_k2 = KeyRing({2: fixtures.FIXED_KEYS[2]})
    only_k1 = KeyRing({1: fixtures.FIXED_KEYS[1]})
    out = {}
    for scenario in ("baseline", "block_tampered", "truncated",
                     "bigblock", "smallblock", "conversion_check"):
        out[(scenario, V1, V1)] = Reader.v1(full)
        out[(scenario, V2, V1)] = Reader.v2(full)
        out[(scenario, V1, V2)] = Reader.v1(full)
        out[(scenario, V2, V2)] = Reader.v2(full)
    # key_missing：读取方密钥环故意缺少文件声明的 key_version
    out[("key_missing", V1, V1)] = Reader.v1(only_k2)
    out[("key_missing", V2, V1)] = Reader.v2(only_k2)
    out[("key_missing", V1, V2)] = Reader.v1(only_k1)
    out[("key_missing", V2, V2)] = Reader.v2(only_k1)
    return out


def build_cases() -> List[Case]:
    """生成完整兼容矩阵用例。"""
    pt = fixtures.sample_plaintext()
    builders = _builders(pt)
    readers = _readers()
    cases: List[Case] = []

    # 期望表：(scenario, producer_version, reader_version)
    #        -> (Verdict, reject_reason or None)
    expected = {
        # 基线：版本内等价；新程序读旧文件需转换；旧程序读新文件拒绝
        ("baseline", V1, V1): (Verdict.EQUIV, None),
        ("baseline", V2, V1): (Verdict.REJECT, "unsupported_version"),
        ("baseline", V1, V2): (Verdict.CONVERT, None),
        ("baseline", V2, V2): (Verdict.EQUIV, None),

        # 期望表键序为 (场景, 文件版本=生产者, 读取方版本)。
        # 块大小变化（v1 文件，真实块 1024）：
        #  - 声明变大到 2048：R1 超范围拒绝；R2 可读，但文件仍是旧版 => CONVERT
        #  - 声明变小到 512：真实块超长，两个读取方都必须拒绝（禁止静默截断）
        ("bigblock", V1, V1): (Verdict.REJECT, "unsupported_block_size"),
        ("bigblock", V1, V2): (Verdict.CONVERT, None),
        ("smallblock", V1, V1): (Verdict.REJECT, "block_too_large"),
        ("smallblock", V1, V2): (Verdict.REJECT, "block_too_large"),

        # 密钥版本缺失：
        #  R2 读 v1 文件能通过版本门，真实原因为 key_not_found；
        #  R1 读 v2 文件先撞版本门，原因为 unsupported_version（fail-closed）。
        ("key_missing", V1, V1): (Verdict.REJECT, "key_not_found"),
        ("key_missing", V1, V2): (Verdict.REJECT, "key_not_found"),
        ("key_missing", V2, V1): (Verdict.REJECT, "unsupported_version"),
        ("key_missing", V2, V2): (Verdict.REJECT, "key_not_found"),

        # 块被篡改：能到达鉴权阶段的读取方必须报 block_mac_mismatch，绝不返回明文；
        # R1 读 v2 文件先撞版本门，报 unsupported_version（同样是明确拒绝）。
        ("block_tampered", V1, V1): (Verdict.REJECT, "block_mac_mismatch"),
        ("block_tampered", V1, V2): (Verdict.REJECT, "block_mac_mismatch"),
        ("block_tampered", V2, V1): (Verdict.REJECT, "unsupported_version"),
        ("block_tampered", V2, V2): (Verdict.REJECT, "block_mac_mismatch"),

        # 截断：声明块数对不上必须报 truncated；R1 读 v2 先撞版本门。
        ("truncated", V1, V1): (Verdict.REJECT, "truncated"),
        ("truncated", V1, V2): (Verdict.REJECT, "truncated"),
        ("truncated", V2, V1): (Verdict.REJECT, "unsupported_version"),
        ("truncated", V2, V2): (Verdict.REJECT, "truncated"),
    }

    scenario_order = ["baseline", "bigblock", "smallblock",
                      "key_missing", "block_tampered", "truncated"]
    for scenario in scenario_order:
        for file_version in (V1, V2):
            if file_version not in builders[scenario]:
                continue
            for reader_version in (V1, V2):
                verdict, reason = expected[(scenario, file_version, reader_version)]
                cases.append(Case(
                    scenario=scenario,
                    producer=PRODUCER_LABELS[file_version],
                    reader=READER_LABELS[reader_version],
                    expected=verdict,
                    build_blob=builders[scenario][file_version],
                    reader_impl=readers[(scenario, reader_version, file_version)],
                    expected_plaintext=pt,
                    expected_reason=reason,
                ))
    return cases


def run() -> Tuple[List[Cell], ConversionResult]:
    cells = [run_case(c) for c in build_cases()]
    conversion = check_conversion_path()
    return cells, conversion


def check_conversion_path() -> ConversionResult:
    """验证 CONVERT 是真实可执行的路径，而非只打标记：

    R2 读取 v1 样例 -> needs_conversion=True 且内容等价 ->
    用 W2 重写成 v2 -> R2 再读 -> 内容仍与源明文等价。
    """
    pt = fixtures.sample_plaintext()
    ring = fixtures.combined_ring()
    try:
        old_blob = fixtures.load_blob(V1)
        first = Reader.v2(ring).read(old_blob)
        if first.plaintext != pt:
            return ConversionResult(False, "v2 读取 v1 文件内容不等价，无法转换")
        if not first.needs_conversion:
            return ConversionResult(False, "v2 读取 v1 文件未报告 needs_conversion")

        upgraded = Writer.v2(ring.get(2), block_size=2048, key_version=2).write(
            first.plaintext, nonce=fixtures.FIXED_NONCE_V2
        )
        second = Reader.v2(ring).read(upgraded)
        if second.needs_conversion:
            return ConversionResult(False, "重写后的 v2 文件仍报告 needs_conversion")
        if second.plaintext != pt:
            return ConversionResult(False, "转换后再读内容不等价")
        return ConversionResult(
            True,
            f"R2 读取 v1 样例（{len(old_blob)} 字节）→ 等价且 needs_conversion"
            f"=True → W2 重写（{len(upgraded)} 字节）→ R2 再读等价且无需转换；"
            f"SHA256={hashlib.sha256(pt).hexdigest()[:16]}…",
            {"v1_size": len(old_blob), "v2_size": len(upgraded),
             "sha256": hashlib.sha256(pt).hexdigest()[:16]},
        )
    except Exception as exc:  # 转换路径任何失败都必须可见
        return ConversionResult(
            False, f"转换路径检查抛出异常 {type(exc).__name__}: {exc}"
        )
