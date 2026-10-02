"""扩展注册表。

每个扩展声明自己会给 data 消息新增哪些字段；字段名在所有扩展间必须唯一，
这样接收方可以凭字段名反查扩展，判断该字段是否已协商。
"""

EXTENSIONS = {
    # 压缩扩展：data 消息增加 "c"（压缩算法名）字段
    "compression": {"fields": frozenset({"c"})},
    # 优先级扩展：data 消息增加 "prio"（0-9 整数）字段
    "priority": {"fields": frozenset({"prio"})},
}

# 字段名 -> 所属扩展名
FIELD_TO_EXTENSION = {}
for _name, _spec in EXTENSIONS.items():
    for _field in _spec["fields"]:
        if _field in FIELD_TO_EXTENSION:
            raise RuntimeError(f"扩展字段名冲突: {_field}")
        FIELD_TO_EXTENSION[_field] = _name


def known_extension(name):
    return name in EXTENSIONS


def fields_of(name):
    return EXTENSIONS[name]["fields"]


def extension_of_field(field):
    """返回字段所属扩展名；不属于任何扩展则返回 None。"""
    return FIELD_TO_EXTENSION.get(field)
