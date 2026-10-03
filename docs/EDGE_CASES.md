# 边界用例

均由 `tests/` 下自检测试覆盖，共 50 个用例。

## 帧格式（`tests/test_edge_cases.py::MalformedFrameTests`）

| 用例 | 预期 |
|------|------|
| 帧体不是合法 JSON | `ProtocolError: 帧不是合法 JSON` |
| JSON 顶层不是对象（如 `[1,2,3]`） | `ProtocolError: 帧必须是 JSON 对象` |
| 缺少字符串类型的 `type` | `ProtocolError` |
| 4 字节长度前缀超过 1 MiB | `ProtocolError: 帧长度超限` |
| 握手中连接被关闭 | 内置 `EOFError` 上抛 |
| 握手阶段收到 data 帧 | `ProtocolError: 期望 hello` |

## 协商与数据阶段（`test_edge_cases.py::DataPhaseEdgeTests`、`test_session.py`）

| 用例 | 预期 |
|------|------|
| data 帧带未注册的新字段（前向兼容） | 忽略未知字段，基础字段正常解析 |
| 协商成功但 `pri=99` 越界 | `ProtocolError: 非法 pri 字段` |
| 未协商 `zlib` 却出现 `comp` 字段 | `NegotiationContradiction`，`used=['zlib']` |
| offer 里重复列扩展 | 去重后正常协商 |
| 双方 offer 均为空 | 交集为空，仍走 v2 finish 握手（区别于 legacy） |
| 握手未完成就收发 data | `ProtocolError` |
| `comp=zlib` 但 payload 是损坏数据 | `ProtocolError: zlib 解压失败` |
| 本端调用未协商扩展的 API（`send_data(compress=True)`） | `ExtensionNotNegotiated`，线上一帧不发 |
| 构造时 `required` 不在 `offered` 中 | `NegotiationContradiction` |
| 构造时声明未注册扩展 | `ProtocolError` |
| role 非 client/server | `ValueError` |
| `priority=9` 本地非法值 | `ValueError`，不发送 |

## 支持度三档（全部支持 / 部分支持 / 完全不支持）

- 全部支持：`tests/test_session.py::FullSupportTests`、`test_interop.py::test_new_new_full`
- 部分支持：`PartialSupportTests`、`test_new_new_partial`（交集之外的字段 API 与线路均不可见）
- 完全不支持：`test_new_new_none`（v2 空交集）与 `test_legacy_*`（旧版，legacy 模式）

## 篡改（`tests/test_tamper.py`）

- hello `ext` 内容被改 / ack `ext` 被降级：finish 指纹不一致 → `NegotiationTampered`
- `ext` 类型被破坏：`NegotiationContradiction`
- ack 加入从未提供的扩展：`NegotiationContradiction`
- 攻击者重算 finish 指纹：无法同时骗过两端
