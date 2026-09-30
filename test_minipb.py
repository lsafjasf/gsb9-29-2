"""minipb 自测：兼容矩阵 + 边界用例。运行：python3 -m unittest -v"""

import unittest

from minipb import (
    Field, Message, ProtocolError, TruncatedDataError, LimitExceededError,
    InvalidWireTypeError, NestingTooDeepError, VarintTooLongError,
    encode_varint,
)
from schemas import AddressV1, AddressV2, PersonV1, PersonV2


def sample_v1():
    return PersonV1(name="alice", id=7, tags=["x", "y"],
                    address=AddressV1(city="shanghai"))


def sample_v2():
    return PersonV2(name="alice", id=7, tags=["x", "y"],
                    address=AddressV2(city="shanghai", zip="200000"),
                    email="a@b.c", scores=[1, 2, 300],
                    secondary=AddressV2(city="beijing", zip="100000"))


def key(field_number, wire_type):
    return encode_varint(field_number << 3 | wire_type)


# ------------------------------------------------------------ 基础编解码

class TestBasic(unittest.TestCase):
    def test_empty_message(self):
        msg = PersonV1.decode(b"")
        self.assertIsNone(msg.name)
        self.assertEqual(msg.tags, [])
        self.assertEqual(msg.encode(), b"")

    def test_roundtrip_v1(self):
        self.assertEqual(PersonV1.decode(sample_v1().encode()), sample_v1())

    def test_roundtrip_v2(self):
        self.assertEqual(PersonV2.decode(sample_v2().encode()), sample_v2())

    def test_repeated_field_order_preserved(self):
        msg = PersonV1(tags=["a", "b", "c", "a"])
        self.assertEqual(PersonV1.decode(msg.encode()).tags, ["a", "b", "c", "a"])

    def test_duplicate_singular_tag_last_wins(self):
        data = key(1, 2) + encode_varint(1) + b"a" \
             + key(1, 2) + encode_varint(1) + b"b"
        msg = PersonV1.decode(data)
        self.assertEqual(msg.name, "b")

    def test_nested_message(self):
        msg = sample_v2()
        out = PersonV2.decode(msg.encode())
        self.assertEqual(out.address.zip, "200000")
        self.assertEqual(out.secondary.city, "beijing")


# ------------------------------------------------------------ 未知字段

class TestUnknownFields(unittest.TestCase):
    def test_unknown_fields_preserved_on_reencode(self):
        data = sample_v2().encode()
        old_view = PersonV1.decode(data)          # 新版数据 -> 旧版结构
        self.assertEqual(old_view.name, "alice")  # 已知字段正常
        self.assertTrue(old_view.unknown_fields)  # 未知字段被保留
        restored = PersonV2.decode(old_view.encode())
        self.assertEqual(restored, sample_v2())   # 重编码后新版可完整恢复

    def test_reencode_byte_identical_when_appended_fields(self):
        # v2 新增字段编号均大于 v1，且编码按编号排序，因此旧版重编码逐字节一致
        data = sample_v2().encode()
        self.assertEqual(PersonV1.decode(data).encode(), data)

    def test_unknown_nested_message_preserved(self):
        data = sample_v2().encode()  # 字段 7 是旧版不认识的嵌套消息
        old_view = PersonV1.decode(data)
        self.assertEqual(PersonV2.decode(old_view.encode()).secondary.zip,
                         "100000")

    def test_unknown_field_inside_nested_message(self):
        addr = AddressV2(city="sh", zip="200000").encode()
        old_addr = AddressV1.decode(addr)         # zip 对 AddressV1 未知
        self.assertEqual(old_addr.city, "sh")
        self.assertEqual(AddressV2.decode(old_addr.encode()).zip, "200000")

    def test_wire_type_mismatch_treated_as_unknown(self):
        # 字段 1 在 schema 中是 str(LEN)，这里故意给 VARINT：按未知字段保留
        data = key(1, 0) + encode_varint(42)
        msg = PersonV1.decode(data)
        self.assertIsNone(msg.name)
        self.assertEqual(msg.encode(), data)


# ------------------------------------------------------------ 长度校验

class TestLengthValidation(unittest.TestCase):
    def test_declared_len_exceeds_remaining(self):
        data = key(1, 2) + encode_varint(100) + b"ab"
        with self.assertRaises(TruncatedDataError):
            PersonV1.decode(data)

    def test_declared_len_exceeds_limit(self):
        payload = b"x" * 16
        data = key(1, 2) + encode_varint(len(payload)) + payload
        with self.assertRaises(LimitExceededError):
            PersonV1.decode(data, max_len=8)

    def test_two_errors_are_distinguishable(self):
        # 超出剩余数据 -> TruncatedDataError（且不是 LimitExceededError）
        truncated = key(1, 2) + encode_varint(100)
        try:
            PersonV1.decode(truncated, max_len=8)
            self.fail("应当抛错")
        except TruncatedDataError as exc:
            self.assertNotIsInstance(exc, LimitExceededError)
        # 剩余数据足够但超上限 -> LimitExceededError（且不是 TruncatedDataError）
        over_limit = key(1, 2) + encode_varint(16) + b"x" * 16
        try:
            PersonV1.decode(over_limit, max_len=8)
            self.fail("应当抛错")
        except LimitExceededError as exc:
            self.assertNotIsInstance(exc, TruncatedDataError)

    def test_huge_declared_len_no_allocation(self):
        # 声明 ~4GiB 但缓冲区只有几个字节：必须立刻报错，不得按声明长度分配
        data = key(1, 2) + encode_varint(0xFFFFFFFF)
        with self.assertRaises(TruncatedDataError):
            PersonV1.decode(data)
        # 未知字段的声明长度同样受校验
        data = key(99, 2) + encode_varint(0xFFFFFFFFFFFFFFFF)
        with self.assertRaises(TruncatedDataError):
            PersonV1.decode(data)

    def test_unknown_field_len_limit_checked(self):
        data = key(99, 2) + encode_varint(16) + b"x" * 16
        with self.assertRaises(LimitExceededError):
            PersonV1.decode(data, max_len=8)


# ------------------------------------------------------------ 截断数据

class TestTruncation(unittest.TestCase):
    def test_every_strict_prefix_is_safe(self):
        data = sample_v2().encode()
        saw_truncated = 0
        for i in range(len(data)):
            try:
                PersonV2.decode(data[:i])  # 字段边界处截断是合法的更短消息
            except TruncatedDataError:
                saw_truncated += 1
            except ProtocolError as exc:
                self.fail("前缀 %d 抛出了非截断错误: %r" % (i, exc))
        self.assertGreater(saw_truncated, 0)

    def test_truncated_varint_key(self):
        with self.assertRaises(TruncatedDataError):
            PersonV1.decode(b"\x80")

    def test_truncated_length_prefix(self):
        with self.assertRaises(TruncatedDataError):
            PersonV1.decode(key(1, 2) + b"\x80")

    def test_truncated_nested_message(self):
        data = sample_v1().encode()
        with self.assertRaises(TruncatedDataError):
            PersonV1.decode(data[:-1])


# ------------------------------------------------------------ 畸形数据

class TestMalformed(unittest.TestCase):
    def test_varint_too_long(self):
        with self.assertRaises(VarintTooLongError):
            PersonV1.decode(b"\x80" * 11)

    def test_invalid_wire_type(self):
        with self.assertRaises(InvalidWireTypeError):
            PersonV1.decode(key(1, 3) + b"\x00")

    def test_field_number_zero(self):
        with self.assertRaises(InvalidWireTypeError):
            PersonV1.decode(b"\x00")


# ------------------------------------------------------------ 嵌套深度

class Node(Message):
    pass


Node.FIELDS = {
    1: Field(1, "varint", "value"),
    2: Field(2, "message", "child", message_cls=Node),
}
Node._by_name = {f.name: f for f in Node.FIELDS.values()}


def make_chain(depth):
    node = Node(value=depth)
    for i in range(depth - 1, -1, -1):
        node = Node(value=i, child=node)
    return node


class TestNestingDepth(unittest.TestCase):
    def test_deep_nesting_within_limit(self):
        data = make_chain(300).encode()
        msg = Node.decode(data, max_depth=300)
        depth = 0
        while msg.child is not None:
            msg = msg.child
            depth += 1
        self.assertEqual(depth, 300)

    def test_deep_nesting_exceeds_limit(self):
        data = make_chain(300).encode()
        with self.assertRaises(NestingTooDeepError):
            Node.decode(data, max_depth=64)

    def test_default_depth_limit(self):
        data = make_chain(150).encode()
        with self.assertRaises(NestingTooDeepError):
            Node.decode(data)


# ------------------------------------------------------------ 兼容矩阵

class TestCompatMatrix(unittest.TestCase):
    """写 {v1, v2} x 读 {v1, v2} 四宫格对拍。"""

    def test_v1_write_v1_read(self):
        self.assertEqual(PersonV1.decode(sample_v1().encode()), sample_v1())

    def test_v1_write_v2_read(self):
        msg = PersonV2.decode(sample_v1().encode())
        self.assertEqual(msg.name, "alice")       # 已知字段完整
        self.assertIsNone(msg.email)              # 新字段取默认值
        self.assertEqual(msg.scores, [])
        self.assertIsNone(msg.secondary)
        self.assertIsNone(msg.address.zip)

    def test_v2_write_v1_read(self):
        old_view = PersonV1.decode(sample_v2().encode())
        self.assertEqual((old_view.name, old_view.id, old_view.tags),
                         ("alice", 7, ["x", "y"]))      # 已知字段完整
        self.assertEqual(old_view.address.city, "shanghai")
        # 顶层未知字段 = email(1) + scores(3 次重复) + secondary(1)；
        # zip 作为未知字段保留在嵌套 address 内部
        self.assertEqual(len(old_view.unknown_fields), 5)
        self.assertEqual(len(old_view.address.unknown_fields), 1)
        # 旧版重编码 -> 新版再读：数据零丢失
        self.assertEqual(PersonV2.decode(old_view.encode()), sample_v2())

    def test_v2_write_v2_read(self):
        self.assertEqual(PersonV2.decode(sample_v2().encode()), sample_v2())


if __name__ == "__main__":
    unittest.main(verbosity=2)
