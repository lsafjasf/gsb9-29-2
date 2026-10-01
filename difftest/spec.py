"""协议适配接口：把待测协议接入差分框架。

接入一个真实协议时，实现 ``ProtocolSpec`` 的子类：

- ``random_message`` / ``serialize``（必须实现）：生成随机结构化报文树并序列化；
- ``boundary_message`` / ``nesting_message`` / ``mutate_tree``（可选）：
  分别对应长度边界、嵌套变化、字段增删三类针对性生成；
- ``edge_case``（可选）：生成四类极端情形
  （empty / oversized / deep / all_illegal）；
- ``byte_mutations``（已有默认实现）：在合法报文上制造非法序列；
- ``stats``（可选）：报告报文树的深度/字段数，供覆盖率统计。
"""
from __future__ import annotations


class ProtocolSpec:
    """协议适配器基类。"""

    name = "abstract"

    # ------------------------------------------------------------------
    # 必须实现
    # ------------------------------------------------------------------
    def random_message(self, rng, cfg):
        """返回 ``(tree, meta)``：一棵随机结构化报文树。"""
        raise NotImplementedError

    def serialize(self, tree):
        """把报文树序列化为 ``bytes``。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 可选钩子
    # ------------------------------------------------------------------
    def boundary_message(self, rng, cfg):
        """长度边界用例（长度 0/1/最大值 等）。默认退化为随机生成。"""
        return self.random_message(rng, cfg)

    def nesting_message(self, rng, cfg):
        """嵌套变化用例（深度跨越实现限制）。默认退化为随机生成。"""
        return self.random_message(rng, cfg)

    def mutate_tree(self, tree, rng, cfg):
        """字段增删改。默认不做修改，meta 中注明 noop。"""
        return tree, {"mutation": "noop"}

    def edge_case(self, kind, rng, cfg):
        """四类极端情形。返回 None 表示该协议未实现该情形。"""
        return None

    def stats(self, tree):
        """报文树统计，例如 {"depth": 3}，供覆盖率提示使用。"""
        return {}

    def byte_mutations(self, data, rng):
        """在序列化后的字节流上制造非法序列。

        返回 ``[(tag, bytes), ...]``，框架会随机挑一个使用。
        """
        mutations = []
        if data:
            i = rng.randrange(len(data))
            flipped = bytearray(data)
            flipped[i] ^= 1 << rng.randrange(8)
            mutations.append(("flip", bytes(flipped)))

            cut = rng.randrange(len(data))
            mutations.append(("truncate", data[:cut]))

            j = rng.randrange(len(data))
            shifted = bytearray(data)
            shifted[j] = (shifted[j] + rng.choice((-1, 1))) & 0xFF
            mutations.append(("corrupt_byte", bytes(shifted)))

            k = rng.randrange(len(data) + 1)
            mutations.append(
                ("insert", data[:k] + rng.randbytes(rng.randint(1, 4)) + data[k:])
            )

            if len(data) > 1:
                m = rng.randrange(len(data))
                mutations.append(("delete", data[:m] + data[m + 1 :]))

        mutations.append(("garbage", rng.randbytes(rng.randint(0, 64))))
        return mutations
