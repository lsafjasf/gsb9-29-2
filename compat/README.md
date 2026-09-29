# 加密文件格式跨版本兼容性测试框架

纯 Python 3 标准库实现，无第三方依赖。回答两个问题：**旧程序读新文件会怎样**、
**新程序能否读旧文件**，并覆盖块大小变化、密钥版本缺失、块被篡改三类情形。

## 运行方式

```bash
cd <仓库根目录>
python3 -m compat gen-samples            # 生成/保留各版本样例文件与密钥（samples/）
python3 -m compat matrix                 # 跑全部组合，输出兼容矩阵到 compat_matrix.md
python3 -m compat selftest               # 框架自测（退出码 0=通过，1=失败）
```

`matrix` 在样例缺失时会自动生成；矩阵有任何一格 FAIL 时退出码为 1，可直接接入 CI。

## 被测对象与契约

被测的是"读取器"：签名为 `read(blob, keyring) -> ReadOk`，只允许两种行为：

1. 返回 `ReadOk(plaintext, converted, note)` —— 读取成功；做了格式转换必须置
   `converted=True`（如 v2 读取器把 v1 的 1024 字节块重切分为 4096 布局）；
2. 抛出 `RejectError(reason, detail)` —— 明确拒绝，原因码限
   `UNSUPPORTED_VERSION` / `KEY_UNAVAILABLE` / `INTEGRITY` / `MALFORMED`。

仓库内置两个版本的参考实现：`compat/format_v1.py`（块 1024）与
`compat/format_v2.py`（块 4096、文件头新增 flags 字段、向后兼容读 v1）。
接入真实实现时，只需把各版本的读取函数包成上述契约放进 `readers` 字典。

## 通过判据（每格必须严格匹配期望，读到部分内容不算通过）

| 实际结果 | 含义 |
|---|---|
| `ok_equal` | 读取成功，明文与基准**逐字节等价**，未做转换 |
| `ok_converted` | 读取成功、内容等价，但实现报告做了格式转换 |
| `rejected:<原因码>` | 明确拒绝，且原因码与期望一致 |
| `fail_partial` | 只返回了部分内容（是基准明文的前缀但更短）——**判失败** |
| `fail_content` | 返回内容与基准不符 —— 判失败 |
| `fail_crash` | 崩溃或违反契约 —— 判失败 |

判定规则：

- 期望等价读取时，只有 `ok_equal` 通过；报告了转换也算失败（防止"碰巧等价"）。
- 期望转换读取时，只有 `ok_converted` 通过。
- 期望拒绝时，**原因码必须一致**：例如旧版读取器对 v2 文件应以
  `UNSUPPORTED_VERSION` 拒绝；若误报 `INTEGRITY` 会误导运维，同样判失败。
- 版本门先于完整性/密钥检查：旧版读取器无法解析新版文件头，因此"旧读新"
  的篡改/缺密钥用例，期望拒绝原因是 `UNSUPPORTED_VERSION`。

## 测试场景与覆盖

样例（`samples/`，确定性生成，密钥在 `samples/keys.json`）：

- 每个版本：`payload.bin`（判等基准）、`sample.enc`（正常）、
  `sample.tampered.enc`（第 1 块密文翻转 1 字节）、
  `sample.missingkey.enc`（用 kv9 加密，而密钥环只有 kv1/kv2）。

矩阵组合（18 格）：

- **存档样例读取**：2 读取器 × 2 文件版本 × {正常, 块被篡改, 密钥版本缺失}；
- **跨版本写入回读**：v1 写、v2 写、v2 降级写 v1，分别交两个读取器回读。

三类重点情形的覆盖位置：

- **块大小变化**（1024→4096）：v2 读取器读 v1 样例 = `可读·需转换`；
  v1 读取器读 v2 样例（文件头布局也变了）= `拒绝·UNSUPPORTED_VERSION`；
- **密钥版本缺失**：两读取器 × 两版本的 `missingkey` 样例 =
  `拒绝·KEY_UNAVAILABLE`；
- **块被篡改**：两读取器 × 两版本的 `tampered` 样例 = `拒绝·INTEGRITY`，
  任何返回部分明文的行为都会落入 `fail_partial` 而判失败。

## 自测（`python3 -m compat selftest`）

1. 参考实现跑完整矩阵：18 格必须全部 PASS（矩阵结论符合预期语义）；
2. 三个故意写错的实现（`compat/buggy.py`）必须被框架判失败：
   - A：块校验失败后把已解出的前缀当成功返回 → 必须判 `fail_partial`；
   - B：跳过 HMAC 校验 → 篡改文件必须判 `fail_content`；
   - C：不检查版本号、一律按 v1 布局解析 → 读 v2 文件必须判 FAIL
     （垃圾内容或错误拒绝原因）。
3. 任一条件不满足则自测失败、退出码 1。
