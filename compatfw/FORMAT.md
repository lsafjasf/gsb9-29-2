# 参考加密容器格式 v1 / v2（被测对象）

为了让兼容性测试有真实载体，本框架自带一个最小但完整的加密文件格式
（仅用 Python 3 标准库：`hashlib` / `hmac` / `struct`）。
**其中的密钥流构造仅用于演示兼容性测试方法，不可用于生产。**

## 容器布局

```
+-------------------- 头部 --------------------+
| magic "ENCDEMO"(7B) | version(1B)            |
| key_version(u16)    | block_size(u16)        |
| block_count(u64)                             |
| [v2 独有] file_nonce(16B)                    |
+----------------------------------------------+
| header_hmac = HMAC-SHA256(hdr_key, 头部)(32B)|
+-------------------- 数据块 × N ---------------+
| index(u64) | length(u64) | ciphertext(length)|
| block_hmac = HMAC-SHA256(blk_key,            |
|              version||index||length||ct)(32B)|
+----------------------------------------------+
```

- v1 头部 20 字节；v2 头部 36 字节（多 16 字节 `file_nonce`）。
- 无填充：尾块按真实长度记录，`block_count = ceil(len/block_size)`。
- 密钥派生：HKDF-SHA256（RFC 5869），salt=`encdemo-kdf-v`+版本+key_version，
  分出 `hdr-mac`、`enc`、`blk-mac` 三个子密钥。
- 密钥流：`HMAC-SHA256(enc_key, nonce || be64(block_index))`，与明文异或。

## 两个版本的差异

| 项 | v1 | v2 |
| --- | --- | --- |
| 文件 nonce | 无（固定 16 个 0） | 头内 16B 随机 nonce |
| 默认块大小 | 1024 | 2048 |
| 读取方支持块大小上限 | 1024 | 4096 |
| 读取方接受的文件版本 | 仅 v1 | v1 与 v2（读 v1 报 `needs_conversion`） |

因此：

- **旧程序读新文件**：v1 读取器遇到 version=2，在版本门明确拒绝
  （`unsupported_version`），不会产生任何明文。
- **新程序读旧文件**：v2 读取器接受 v1 文件，内容等价但报告
  `needs_conversion=True`（对应 `CONVERT`），可用 v2 重写升级。

## 读取检查顺序（决定拒绝原因码）

1. 魔数 / 版本号 / 版本是否受支持
2. 按 `key_version` 从密钥环取主密钥（缺失 → `key_not_found`）
3. 头部 HMAC（头被改 → `header_mac_mismatch`）
4. 块大小范围（`unsupported_block_size`）
5. 逐块：序号连续、长度不超块大小（`block_too_large`）、截断（`truncated`）、
   块 HMAC（`block_mac_mismatch`，使用恒定时间比较）
6. 尾部多余字节（`trailing_data`）
