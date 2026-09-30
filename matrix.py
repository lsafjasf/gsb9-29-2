"""实际执行 写{v1,v2} x 读{v1,v2} 四种组合，打印兼容矩阵。

运行：python3 matrix.py
"""

from schemas import AddressV1, AddressV2, PersonV1, PersonV2
from test_minipb import sample_v1, sample_v2


def check_v1_v1():
    return PersonV1.decode(sample_v1().encode()) == sample_v1()


def check_v1_v2():
    msg = PersonV2.decode(sample_v1().encode())
    return (msg.name == "alice" and msg.id == 7 and msg.tags == ["x", "y"]
            and msg.address.city == "shanghai"
            and msg.email is None and msg.scores == []
            and msg.secondary is None and msg.address.zip is None)


def check_v2_v1():
    old_view = PersonV1.decode(sample_v2().encode())
    known_ok = (old_view.name == "alice" and old_view.id == 7
                and old_view.tags == ["x", "y"]
                and old_view.address.city == "shanghai")
    # 旧版重编码 -> 新版再读，数据零丢失
    restored = PersonV2.decode(old_view.encode())
    return known_ok and restored == sample_v2()


def check_v2_v2():
    return PersonV2.decode(sample_v2().encode()) == sample_v2()


ROWS = [
    ("写 v1", "读 v1", "完全相等", check_v1_v1),
    ("写 v1", "读 v2", "已知字段相等；新字段为默认值", check_v1_v2),
    ("写 v2", "读 v1", "已知字段相等；未知字段保留，重编码后 v2 完整恢复",
     check_v2_v1),
    ("写 v2", "读 v2", "完全相等", check_v2_v2),
]

if __name__ == "__main__":
    print("| 组合 | 预期 | 实测 |")
    print("| --- | --- | --- |")
    failed = False
    for writer, reader, expect, fn in ROWS:
        ok = fn()
        failed |= not ok
        print("| %s -> %s | %s | %s |"
              % (writer, reader, expect, "PASS" if ok else "FAIL"))
    raise SystemExit(1 if failed else 0)
