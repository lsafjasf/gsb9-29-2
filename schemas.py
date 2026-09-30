"""新旧两个版本的 schema，用于跨版本兼容对拍。

v1 -> v2 的演进：
    Address 增加字段 2 (zip)
    Person  增加字段 5 (email)、6 (scores, repeated)、7 (secondary, 嵌套消息)
"""

from minipb import Field, Message


# ---------------------------------------------------------------- v1（旧版）

class AddressV1(Message):
    FIELDS = {
        1: Field(1, "str", "city"),
    }


class PersonV1(Message):
    FIELDS = {
        1: Field(1, "str", "name"),
        2: Field(2, "varint", "id"),
        3: Field(3, "str", "tags", repeated=True),
        4: Field(4, "message", "address", message_cls=AddressV1),
    }


# ---------------------------------------------------------------- v2（新版）

class AddressV2(Message):
    FIELDS = {
        1: Field(1, "str", "city"),
        2: Field(2, "str", "zip"),           # 新增
    }


class PersonV2(Message):
    FIELDS = {
        1: Field(1, "str", "name"),
        2: Field(2, "varint", "id"),
        3: Field(3, "str", "tags", repeated=True),
        4: Field(4, "message", "address", message_cls=AddressV2),
        5: Field(5, "str", "email"),                       # 新增
        6: Field(6, "varint", "scores", repeated=True),    # 新增
        7: Field(7, "message", "secondary",                # 新增（嵌套）
                 message_cls=AddressV2),
    }
