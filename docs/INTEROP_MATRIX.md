# 互操作矩阵

协议版本：v1 = 旧版（无协商字段），v2 = 新版（hello 携带 `ext` / `ext_required`）。
内置扩展：`priority`（data 帧加 `pri` 字段）、`zlib`（data 帧加 `comp` 字段并压缩 payload）。

## 版本组合矩阵

| # | 客户端 | 服务端 | 协商结果 | 后续行为 | 对应测试 |
|---|--------|--------|----------|----------|----------|
| 1 | v2（priority+zlib） | v2（priority+zlib） | `{priority, zlib}` | data 帧可带 `pri`/`comp`，握手含 finish 指纹 | `test_new_new_full` |
| 2 | v2（priority+zlib） | v2（priority） | `{priority}` | 仅 `pri` 可用；`compress=True` 抛 `ExtensionNotNegotiated` | `test_new_new_partial` |
| 3 | v2（priority+zlib） | v2（zlib） | `{zlib}` | 仅 `comp` 可用 | `test_new_new_partial`（反向） |
| 4 | v2（空 offer） | v2（空 offer） | `{}` | 基础协议行为，仍是 v2 握手（有 finish） | `test_new_new_none` |
| 5 | v2（required=zlib） | v2（priority） | **失败** | 双方抛 `NegotiationFailed`，带双方能力清单 | `test_required_missing_fails_both_sides_with_capabilities` |
| 6 | v1（旧版） | v2 | `{}`（legacy） | 服务端检测 hello 无 `ext` → 回 `proto:1` ack，基础协议 | `test_legacy_client_new_server` |
| 7 | v2 | v1（旧版） | `{}`（legacy） | 客户端检测 ack 无 `ext` → 基础协议，无 finish | `test_new_client_legacy_server` |
| 8 | v1 | v1 | — | 纯基础协议 | `test_legacy_legacy` |
| 9 | v2（required=zlib, `allow_legacy=False`） | v1 | **失败** | `NegotiationFailed`（默认 `allow_legacy=True` 时静默降级） | `test_required_ext_vs_legacy_server` |

## 关键兼容规则

- **老对端识别**：`hello` 缺 `ext` 字段，或 `hello_ack` 缺 `ext` / `proto < 2`，即按 v1 基础协议运行，不交换 finish。
- **未知字段忽略**：v1 端点收到 v2 hello 时忽略 `ext`/`ext_required`/`nonce`；v2 端点收到未注册的帧字段（未来扩展）也忽略，保证前向兼容。
- **已知但未协商的字段拒绝**：帧中出现注册过但未协商成功的扩展字段（如未协商 `priority` 却带 `pri`）→ `NegotiationContradiction`。
- **降级与必需扩展**：默认允许降级到 v1；若业务不能接受静默降级，构造 `Session(..., allow_legacy=False)`。

## 篡改检测矩阵（MITM 修改协商字段）

| 篡改方式 | 检测结果 |
|----------|----------|
| 修改 hello 的 `ext` 内容（保留字段） | 双方 finish 指纹不一致 → 双方 `NegotiationTampered` |
| 破坏 `ext` 类型（字符串代替数组） | 服务端 `NegotiationContradiction` 并回 error 帧 |
| 修改 ack 的 `ext`（增/删扩展） | 客户端指纹不一致 → `NegotiationTampered`；若 ack 含未提供扩展 → `NegotiationContradiction` |
| 攻击者按篡改后视图重算 finish 指纹 | 至多骗过一端，另一端指纹必然不一致（无共享密钥下的理论上限） |
| 整体剥离 `ext` 字段（模拟旧端） | 无法与真实旧端区分，静默降级；用 `allow_legacy=False` 拒绝 |
