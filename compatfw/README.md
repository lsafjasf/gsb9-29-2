# 加密文件格式跨版本兼容性测试框架

对“加密文件格式版本变更”做**系统化**跨版本验证：为每个版本保留样例文件与密钥，
测试全部「生产者版本 × 读取方版本 × 异常场景」组合，对每格给出三值结论与依据。
Python 3 标准库实现，无第三方依赖。

- 判据说明：见 [CRITERIA.md](CRITERIA.md)
- 被测格式说明：见 [FORMAT.md](FORMAT.md)
- 兼容矩阵（Markdown）：见 [reports/compat_matrix.md](reports/compat_matrix.md)
- 终端矩阵存档：[reports/report.txt](reports/report.txt)
- 机器可读结果：[reports/report.json](reports/report.json)
- 自测结果：[reports/selftest.txt](reports/selftest.txt)

## 目录结构

```
compatfw/
├── run_compat.py              # CLI：matrix / gen-fixtures / selftest
├── compat/
│   ├── errors.py              # 明确拒绝异常体系（带 reason 原因码）
│   ├── crypto.py              # HKDF/HMAC/密钥流（标准库；密钥流仅演示用）
│   ├── codec.py               # v1/v2 容器格式：Writer/Reader/KeyRing
│   ├── verdicts.py            # EQUIV/CONVERT/REJECT/PARTIAL/FAIL/ERROR/NA
│   ├── harness.py             # 通用判定引擎（适配器协议，不绑定具体格式）
│   ├── fixtures.py            # 每版本样例文件、密钥、场景变体生成/加载
│   ├── matrix.py              # 用例构建、期望表、转换路径检查
│   └── report.py              # 终端/Markdown/JSON 报告渲染
├── fixtures/
│   ├── v1/  sample.enc keys.json plaintext.bin manifest.json
│   ├── v2/  （同上）
│   └── scenarios/ key_missing_* tampered_* truncated_* bigblock_v1.enc
├── tests/
│   ├── test_matrix.py                  # 两版本样例自测：矩阵结论符合预期
│   └── test_buggy_implementations.py  # 注入错误实现，断言框架报失败
└── reports/                   # 矩阵与自测结果存档
```

## 运行方式

```bash
cd compatfw

python3 run_compat.py gen-fixtures            # （重新）生成两版本样例与密钥
python3 run_compat.py matrix                  # 运行兼容矩阵，终端输出
python3 run_compat.py matrix --md out.md --json out.json   # 同时导出报告
python3 run_compat.py selftest                # 框架自测（含错误实现检测）
# 也可直接用 unittest：
python3 -m unittest discover -s tests -v
```

退出码：矩阵命令 `0`=所有格子符合预期且转换路径通过；`1`=存在不符合项。
自测命令 `0`=14 个测试全部通过。

## 兼容矩阵结论（20 格 + 1 项转换路径检查）

判定：**EQUIV** 可读且内容等价（通过）；**CONVERT** 可读但需转换（通过）；
**REJECT** 明确拒绝并给出原因（拒绝类用例的通过结果）。
PARTIAL / FAIL / ERROR 均不通过。

| 场景 | W→R | 结论 | 拒绝原因 |
| --- | --- | --- | --- |
| 基线 | W1→R1 | EQUIV | — |
| 基线 | W1→R2 | CONVERT | — |
| 基线 | W2→R1 | REJECT | unsupported_version（旧程序读新文件） |
| 基线 | W2→R2 | EQUIV | — |
| 块大小变大 1024→2048 | W1→R1 | REJECT | unsupported_block_size |
| 块大小变大 1024→2048 | W1→R2 | CONVERT | —（R2 上限 4096，文件仍为 v1 需转换） |
| 块大小变小 1024→512 | W1→R1/R2 | REJECT | block_too_large（禁止静默截断） |
| 密钥版本缺失 | W1→R1/W1→R2/W2→R2 | REJECT | key_not_found |
| 密钥版本缺失 | W2→R1 | REJECT | unsupported_version（先撞版本门） |
| 块被篡改 | W1→R1/W1→R2/W2→R2 | REJECT | block_mac_mismatch |
| 块被篡改 | W2→R1 | REJECT | unsupported_version（先撞版本门） |
| 文件截断 | W1→R1/W1→R2/W2→R2 | REJECT | truncated |
| 文件截断 | W2→R1 | REJECT | unsupported_version（先撞版本门） |
| 转换路径 | R2 读 v1→W2 重写→R2 再读 | 通过 | 内容全程等价，`needs_conversion` 正确翻转 |

统计：EQUIV=2，CONVERT=2，REJECT=16，不符合期望=0。

## 框架自测如何“对错误实现报失败”

`tests/test_buggy_implementations.py` 注入四类错误读取器，框架必须稳定抓到：

1. `SilentTruncateReader` 静默少返回 1 字节 → 判定 **FAIL**（内容不等价）
2. `PartialReadReader` 带 1024 字节部分明文后抛异常 → 判定 **PARTIAL**（不算通过）
3. `LenientMacReader` 块 HMAC 失败仍解密返回 → 判定 **FAIL**（篡改文件竟可读）
4. `CrashyReader` 抛裸 `RuntimeError` → 判定 **ERROR**（实现缺陷，非明确拒绝）

另含对照测试（正确实现仍得 CONVERT）与变异验证：把预期判据改错时
`selftest` 立即变红（实测报 2 个失败），恢复后全绿，证明测试真实有效。

## 复用框架测试自己的格式

`compat/harness.py` 不依赖具体加密实现。接入新格式只需：

- 写入器提供 `produce(plaintext) -> bytes`；
- 读取器提供 `read(blob) -> 含 plaintext / needs_conversion 的结果`；
- 明确拒绝时抛出**带 `reason` 字符串**的异常；若存在部分明文外泄，
  在异常上设置 `partial_plaintext` 属性（框架将判 PARTIAL）。

然后参照 `compat/matrix.py` 构建 `Case` 列表（含期望判定与期望原因码），
用 `run_matrix` / `summarize` 得到矩阵。
