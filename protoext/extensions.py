"""扩展注册表与内置扩展。

每个扩展声明自己会给数据帧增加哪些字段（``frame_fields``），
编码/解码钩子只在扩展被协商成功后才会被调用。
"""

import base64
import zlib


class Extension:
    """一个协议扩展。

    - ``name``: 协商时使用的标识符。
    - ``frame_fields``: 该扩展会在 data 帧上引入的字段名集合。
      解码端用它在“未协商却出现”时报矛盾错误。
    - ``encode(frame, options)``: 协商成功后，发送 data 帧前调用，
      就地修改 frame。
    - ``decode(frame)``: 接收 data 帧时调用，就地还原 frame。
    """

    name = ""
    frame_fields = frozenset()

    def encode(self, frame, options):
        raise NotImplementedError

    def decode(self, frame):
        raise NotImplementedError


class PriorityExtension(Extension):
    """为 data 帧增加 0-3 级优先级字段 ``pri``。"""

    name = "priority"
    frame_fields = frozenset({"pri"})

    def encode(self, frame, options):
        pri = options.get("priority")
        if pri is None:
            return
        if not isinstance(pri, int) or not 0 <= pri <= 3:
            raise ValueError("priority 必须是 0-3 的整数")
        frame["pri"] = pri

    def decode(self, frame):
        pri = frame.get("pri")
        if not isinstance(pri, int) or not 0 <= pri <= 3:
            from .errors import ProtocolError
            raise ProtocolError(f"非法 pri 字段: {pri!r}")


class ZlibCompressExtension(Extension):
    """对 payload 做 zlib 压缩，增加 ``comp`` 字段，payload 变为 base64。"""

    name = "zlib"
    frame_fields = frozenset({"comp"})

    def encode(self, frame, options):
        if not options.get("compress"):
            return
        payload = frame.get("payload")
        if not isinstance(payload, str):
            return
        raw = payload.encode("utf-8")
        frame["payload"] = base64.b64encode(zlib.compress(raw)).decode("ascii")
        frame["comp"] = "zlib"

    def decode(self, frame):
        if frame.get("comp") != "zlib":
            from .errors import ProtocolError
            raise ProtocolError(f"未知压缩标识: {frame.get('comp')!r}")
        try:
            raw = zlib.decompress(base64.b64decode(frame["payload"]))
            frame["payload"] = raw.decode("utf-8")
        except Exception as exc:
            from .errors import ProtocolError
            raise ProtocolError(f"zlib 解压失败: {exc}") from exc
        del frame["comp"]


BUILTIN_EXTENSIONS = (PriorityExtension, ZlibCompressExtension)


class ExtensionRegistry:
    def __init__(self, extensions=BUILTIN_EXTENSIONS):
        self._by_name = {}
        for ext_cls in extensions:
            ext = ext_cls()
            self._by_name[ext.name] = ext

    def get(self, name):
        return self._by_name.get(name)

    def names(self):
        return sorted(self._by_name)

    def field_owner(self, field):
        """返回声明了该帧字段的扩展名，未注册返回 None。"""
        for ext in self._by_name.values():
            if field in ext.frame_fields:
                return ext.name
        return None


registry = ExtensionRegistry()
