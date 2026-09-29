"""tlvproto 自测：编解码、跨版本兼容矩阵、边界与畸形输入。

运行：python3 test_tlvproto.py -v
"""

import unittest

import tlvproto as tp
from tlvproto import (
    Field, Message,
    ProtocolError, TruncatedError, LimitExceededError, DepthLimitError,
    UnknownWireTypeError, SchemaError,
    encode, decode, encode_varint,
)


# ------------------------------------------------------------ 测试用 schema

# v1：初版
SCHEMA_V1 = (
    Field(1, 'name', 'string'),
    Field(2, 'age', 'varint'),
)

# v2：新增 email(3)、scores(4, repeated)、avatar(5)
SCHEMA_V2 = (
    Field(1, 'name', 'string'),
    Field(2, 'age', 'varint'),
    Field(3, 'email', 'string'),
    Field(4, 'scores', 'varint', repeated=True),
    Field(5, 'avatar', 'bytes'),
)

# 嵌套：address 里也有 v1/v2 之分，用来验证嵌套层的未知字段保留
ADDR_V1 = (Field(1, 'city', 'string'),)
ADDR_V2 = (Field(1, 'city', 'string'), Field(2, 'zip', 'string'))

USER_V1 = (
    Field(1, 'name', 'string'),
    Field(2, 'address', 'message', schema=ADDR_V1),
)
USER_V2 = (
    Field(1, 'name', 'string'),
    Field(2, 'address', 'message', schema=ADDR_V2),
    Field(3, 'tags', 'string', repeated=True),
)

# 全类型
ALL_KINDS = (
    Field(1, 'u', 'varint'),
    Field(2, 'f32', 'fixed32'),
    Field(3, 'f64', 'fixed64'),
    Field(4, 'blob', 'bytes'),
    Field(5, 'text', 'string'),
)


def tag(number, wire):
    return encode_varint(number << 3 | wire)


# ------------------------------------------------------------ 基础编解码

class TestRoundTrip(unittest.TestCase):
    def test_empty_message(self):
        self.assertEqual(encode(SCHEMA_V1, {}), b'')
        msg = decode(SCHEMA_V1, b'')
        self.assertEqual(msg.fields, {})
        self.assertEqual(msg.unknown, [])

    def test_all_kinds_round_trip(self):
        obj = {'u': 2 ** 64 - 1, 'f32': 0xDEADBEEF, 'f64': 2 ** 64 - 2,
               'blob': b'\x00\xff\x10', 'text': '你好, world'}
        data = encode(ALL_KINDS, obj)
        self.assertEqual(decode(ALL_KINDS, data).fields, obj)

    def test_nested_round_trip(self):
        obj = {'name': 'alice',
               'address': {'city': '上海', 'zip': '200000'},
               'tags': ['a', 'b', 'c']}
        data = encode(USER_V2, obj)
        msg = decode(USER_V2, data)
        self.assertEqual(msg.fields['name'], 'alice')
        self.assertEqual(msg.fields['address'].fields, obj['address'])
        self.assertEqual(msg.fields['tags'], ['a', 'b', 'c'])

    def test_repeated_fields(self):
        obj = {'name': 'x', 'age': 1, 'scores': [90, 80, 70]}
        data = encode(SCHEMA_V2, obj)
        self.assertEqual(decode(SCHEMA_V2, data).fields['scores'], [90, 80, 70])

    def test_repeated_tags_interleaved_on_wire(self):
        # 线上同号字段被其他字段隔开，仍应聚合为一个列表
        wire = (tag(4, tp.WIRE_VARINT) + encode_varint(1)
                + tag(1, tp.WIRE_LEN) + encode_varint(1) + b'x'
                + tag(4, tp.WIRE_VARINT) + encode_varint(2))
        msg = decode(SCHEMA_V2, wire)
        self.assertEqual(msg.fields['scores'], [1, 2])
        self.assertEqual(msg.fields['name'], 'x')

    def test_varint_boundaries(self):
        for value in (0, 1, 127, 128, 300, 2 ** 32, 2 ** 63, 2 ** 64 - 1):
            data = encode(SCHEMA_V1, {'age': value})
            self.assertEqual(decode(SCHEMA_V1, data).fields['age'], value)


# ------------------------------------------------------------ 未知字段

class TestUnknownFields(unittest.TestCase):
    def test_unknown_tag_skipped_and_preserved(self):
        data = encode(SCHEMA_V2, {'name': 'a', 'age': 7, 'email': 'a@b.c',
                                  'scores': [1, 2]})
        msg = decode(SCHEMA_V1, data)  # v1 不认识 3/4 号字段
        self.assertEqual(msg.fields, {'name': 'a', 'age': 7})
        self.assertTrue(msg.unknown)
        # 重新编码后未知字段不丢：v2 能完整恢复
        recovered = decode(SCHEMA_V2, encode(SCHEMA_V1, msg))
        self.assertEqual(recovered.fields,
                         {'name': 'a', 'age': 7, 'email': 'a@b.c',
                          'scores': [1, 2]})

    def test_unknown_field_byte_exact_when_last(self):
        # 未知字段原本就在末尾时，重编码字节级一致
        data = encode(SCHEMA_V2, {'name': 'a', 'age': 7, 'email': 'x@y'})
        msg = decode(SCHEMA_V1, data)
        self.assertEqual(encode(SCHEMA_V1, msg), data)

    def test_unknown_field_in_middle_preserved_semantically(self):
        # 未知字段在中间：重编码后顺序可变（移到末尾），但语义不丢
        wire = (tag(9, tp.WIRE_VARINT) + encode_varint(42)
                + tag(1, tp.WIRE_LEN) + encode_varint(1) + b'x')
        msg = decode(SCHEMA_V1, wire)
        self.assertEqual(msg.fields, {'name': 'x'})
        self.assertEqual(len(msg.unknown), 1)
        again = decode(SCHEMA_V1, encode(SCHEMA_V1, msg))
        self.assertEqual(again, msg)

    def test_unknown_fields_of_every_wire_type(self):
        wire = b''.join([
            tag(10, tp.WIRE_VARINT) + encode_varint(1),
            tag(11, tp.WIRE_FIXED64) + b'12345678',
            tag(12, tp.WIRE_LEN) + encode_varint(3) + b'abc',
            tag(13, tp.WIRE_FIXED32) + b'abcd',
        ])
        msg = decode(SCHEMA_V1, wire)
        self.assertEqual(msg.fields, {})
        self.assertEqual(len(msg.unknown), 4)
        self.assertEqual(encode(SCHEMA_V1, msg), wire)

    def test_nested_unknown_fields_preserved(self):
        # 嵌套消息内部的新字段同样要保留
        data = encode(USER_V2, {'name': 'n',
                                'address': {'city': 'c', 'zip': '100000'}})
        msg = decode(USER_V1, data)  # 旧版 address 没有 zip
        self.assertEqual(msg.fields['address'].fields, {'city': 'c'})
        self.assertEqual(len(msg.fields['address'].unknown), 1)
        recovered = decode(USER_V2, encode(USER_V1, msg))
        self.assertEqual(recovered.fields['address'].fields,
                         {'city': 'c', 'zip': '100000'})


# ------------------------------------------------------------ 兼容矩阵

class TestCompatMatrix(unittest.TestCase):
    """writer x reader 四格矩阵，见 README。"""

    V1_OBJ = {'name': 'alice', 'age': 30}
    V2_OBJ = {'name': 'alice', 'age': 30, 'email': 'a@b.c',
              'scores': [88, 99], 'avatar': b'\x89PNG'}

    def test_v1_write_v1_read(self):
        msg = decode(SCHEMA_V1, encode(SCHEMA_V1, self.V1_OBJ))
        self.assertEqual(msg.fields, self.V1_OBJ)
        self.assertEqual(msg.unknown, [])

    def test_v1_write_v2_read(self):
        # 旧数据缺少新字段：新字段缺席，由上层给默认值
        msg = decode(SCHEMA_V2, encode(SCHEMA_V1, self.V1_OBJ))
        self.assertEqual(msg.fields, self.V1_OBJ)
        self.assertNotIn('email', msg.fields)
        self.assertNotIn('scores', msg.fields)

    def test_v2_write_v1_read_then_forward(self):
        # 新版数据 -> 旧版解析 -> 旧版重编码 -> 新版解析：零丢失
        data = encode(SCHEMA_V2, self.V2_OBJ)
        old_view = decode(SCHEMA_V1, data)
        self.assertEqual(old_view.fields, {'name': 'alice', 'age': 30})
        forwarded = encode(SCHEMA_V1, old_view)
        new_view = decode(SCHEMA_V2, forwarded)
        self.assertEqual(new_view.fields, self.V2_OBJ)

    def test_v2_write_v2_read(self):
        msg = decode(SCHEMA_V2, encode(SCHEMA_V2, self.V2_OBJ))
        self.assertEqual(msg.fields, self.V2_OBJ)


# ------------------------------------------------------------ 长度校验

class TestLengthValidation(unittest.TestCase):
    def test_declared_length_exceeds_remaining(self):
        # 声明 100 字节，实际只剩 1 字节
        wire = tag(1, tp.WIRE_LEN) + encode_varint(100) + b'x'
        with self.assertRaises(TruncatedError):
            decode(SCHEMA_V1, wire)

    def test_declared_length_exceeds_limit(self):
        # 声明长度超上限：与截断是可区分的另一种错误
        wire = tag(1, tp.WIRE_LEN) + encode_varint(9) + b'123456789'
        with self.assertRaises(LimitExceededError):
            decode(SCHEMA_V1, wire, max_length=8)
        # 同样的数据在上限内则正常
        self.assertEqual(decode(SCHEMA_V1, wire, max_length=9).fields['name'],
                         '123456789')

    def test_limit_checked_before_remaining(self):
        # 同时超上限和超剩余时，报上限错误（先查上限，不做任何分配）
        wire = tag(1, tp.WIRE_LEN) + encode_varint(2 ** 40)
        with self.assertRaises(LimitExceededError):
            decode(SCHEMA_V1, wire, max_length=1024)

    def test_unknown_field_length_also_validated(self):
        wire = tag(99, tp.WIRE_LEN) + encode_varint(10 ** 9)
        with self.assertRaises(LimitExceededError):
            decode(SCHEMA_V1, wire, max_length=1024)
        wire2 = tag(99, tp.WIRE_LEN) + encode_varint(50) + b'ab'
        with self.assertRaises(TruncatedError):
            decode(SCHEMA_V1, wire2)

    def test_nested_length_validated_against_parent(self):
        # 内层声明长度不得超过外层负载的剩余
        inner = tag(1, tp.WIRE_LEN) + encode_varint(100) + b'x'
        outer = tag(2, tp.WIRE_LEN) + encode_varint(len(inner)) + inner
        with self.assertRaises(TruncatedError):
            decode(USER_V1, outer)


# ------------------------------------------------------------ 截断

class TestTruncation(unittest.TestCase):
    SAMPLE = encode(SCHEMA_V2, {'name': 'alice', 'age': 300,
                                'email': 'a@b.c', 'scores': [1, 2, 3],
                                'avatar': b'\x00\x01\x02'})

    def test_every_prefix_is_safe(self):
        # 任意前缀：要么恰好落在字段边界上解析成功，要么 TruncatedError，
        # 绝不抛其它异常、绝不崩溃
        for i in range(len(self.SAMPLE)):
            try:
                decode(SCHEMA_V2, self.SAMPLE[:i])
            except TruncatedError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.fail(f'前缀 {i} 抛出了非截断异常: {exc!r}')

    def test_truncated_varint(self):
        # 300 的 varint 是 2 字节，只给 1 字节（且带续位）
        wire = tag(2, tp.WIRE_VARINT) + bytes([0xAC])
        with self.assertRaises(TruncatedError):
            decode(SCHEMA_V1, wire)

    def test_truncated_fixed32_fixed64(self):
        schema = (Field(1, 'a', 'fixed32'), Field(2, 'b', 'fixed64'))
        with self.assertRaises(TruncatedError):
            decode(schema, tag(1, tp.WIRE_FIXED32) + b'\x01\x02')
        with self.assertRaises(TruncatedError):
            decode(schema, tag(2, tp.WIRE_FIXED64) + b'\x01\x02\x03')

    def test_truncated_unknown_fixed(self):
        with self.assertRaises(TruncatedError):
            decode(SCHEMA_V1, tag(9, tp.WIRE_FIXED64) + b'\x00')


# ------------------------------------------------------------ 深度

class TestDepth(unittest.TestCase):
    @staticmethod
    def _nest(depth):
        payload = b''
        for _ in range(depth):
            payload = tag(1, tp.WIRE_LEN) + encode_varint(len(payload)) + payload
        return payload

    @staticmethod
    def _recursive_schema():
        node = []
        node.append(Field(1, 'child', 'message', schema=node))
        return node

    def test_deep_nesting_within_limit(self):
        schema = self._recursive_schema()
        msg = decode(schema, self._nest(50))
        depth = 0
        while 'child' in msg.fields:
            msg = msg.fields['child']
            depth += 1
        self.assertEqual(depth, 50)

    def test_excessive_nesting_rejected(self):
        schema = self._recursive_schema()
        with self.assertRaises(DepthLimitError):
            decode(schema, self._nest(150))           # 默认上限 100
        with self.assertRaises(DepthLimitError):
            decode(schema, self._nest(10), max_depth=5)

    def test_deep_nesting_does_not_hit_recursion_limit(self):
        # 150 层嵌套在深度上限处被拒绝，而不是 RecursionError
        schema = self._recursive_schema()
        try:
            decode(schema, self._nest(5000))
        except DepthLimitError:
            pass
        except RecursionError:
            self.fail('深度限制未生效，触发 RecursionError')


# ------------------------------------------------------------ 畸形输入

class TestMalformed(unittest.TestCase):
    def test_unknown_wire_type(self):
        for wire_type in (3, 4, 6, 7):
            with self.assertRaises(UnknownWireTypeError):
                decode(SCHEMA_V1, tag(9, wire_type))

    def test_field_number_zero(self):
        with self.assertRaises(ProtocolError):
            decode(SCHEMA_V1, b'\x00')

    def test_varint_overflow(self):
        with self.assertRaises(ProtocolError):
            decode(SCHEMA_V1, tag(2, tp.WIRE_VARINT) + b'\x80' * 10 + b'\x01')

    def test_wire_type_mismatch_on_known_field(self):
        # schema 说 name 是 string(LEN)，线上却给了 VARINT
        wire = tag(1, tp.WIRE_VARINT) + encode_varint(1)
        with self.assertRaises(SchemaError):
            decode(SCHEMA_V1, wire)

    def test_invalid_utf8(self):
        wire = tag(1, tp.WIRE_LEN) + encode_varint(2) + b'\xff\xfe'
        with self.assertRaises(ProtocolError):
            decode(SCHEMA_V1, wire)


if __name__ == '__main__':
    unittest.main()
