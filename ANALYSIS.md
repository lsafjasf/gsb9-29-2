# chunkcrypt 加密文件格式 — 安全性分析（不变量清单 + 攻击实验）

- 被测代码：`target/chunkcrypt.py`（316 行，Python 3 标准库），逐字节取自仓库 `origin/56-B:chunkcrypt.py`，
  git blob `39e88abcf6626657b50c0b7f4839317dd12414af`，**未做任何修改**。
- 实验脚本：`experiments/run_attacks.py`（仅标准库），样本输出：`experiments/RESULTS.txt`。
- 运行方式：`python3 experiments/run_attacks.py`（期望退出码 0；构造的篡改样本落在 `experiments/tmp/`）。
- 环境：Python 3.12.3，Linux。

## 1. 格式与数据流（代码位置）

```
头部 96B   magic(8) chunk_size(4) total_size(8) chunk_count(8) kdf_iter(4)
           salt(16) nonce(16) header_mac(32)          ← chunkcrypt.py:51-52
索引区     chunk_count × 32B（每项 = 该块密文的 HMAC）+ index_mac(32)
数据区     chunk[0..N-1]，密文与明文等长（流加密）
```

- 密钥派生：`enc_key/mac_key = HMAC(master, "chunkcrypt/enc"|"chunkcrypt/mac")`，`chunkcrypt.py:85-88`；
  口令路径 PBKDF2-HMAC-SHA256，`chunkcrypt.py:115-126`。
- 保密性：PRF-CTR 流密码，块 `i` 第 `j` 个 32B 密钥流 = `SHA256(enc_key‖nonce‖i‖j)`，`chunkcrypt.py:91-101`。
- 完整性：`chunk_mac[i] = HMAC(mac_key, "chunk"‖i‖ciphertext_i)`，`chunkcrypt.py:110-112`；
  `index_mac = HMAC(mac_key, "index"‖index)`，`chunkcrypt.py:172`；
  `header_mac = HMAC(mac_key, "header"‖header)`，`chunkcrypt.py:156`。
- 打开时校验链：头部 → 索引 → 文件长度，全部在 `ChunkReader._load`，`chunkcrypt.py:203-244`；
  逐块校验是**惰性**的，发生在 `_read_cipher_chunk`，`chunkcrypt.py:260-271`。

## 2. 不变量清单

状态列：✅=代码强制且实验验证成立；⚠️=代码强制但语义有保留；❌=声称/期望但实际不成立。

### 2.1 机密性

| # | 不变量 | 状态 | 证据 |
|---|--------|------|------|
| C1 | 密钥流由 `(enc_key, nonce, chunk_index, counter)` 唯一决定，块内任意偏移可定位 | ✅ | `_keystream` `chunkcrypt.py:91-101` |
| C2 | 每次加密取新随机 16B nonce，同密钥重复加密同明文密文不同 | ✅（概率性） | `nonce = os.urandom(16)` `chunkcrypt.py:152`；nonce 碰撞概率 ≈ 2⁻⁶⁴ 量级，代码无去重/计数器 |
| C3 | 密文与明文等长（流密码），无填充预言面 | ✅ | `_xor` `chunkcrypt.py:104-107` |

### 2.2 完整性

| # | 不变量 | 状态 | 证据 |
|---|--------|------|------|
| I1 | 块 `i` 的 MAC 绑定块号 `i`：块移动到别的位置即失配 | ✅ | `chunkcrypt.py:110-112`；实验 E1b/E2a |
| I2 | 索引区任何改动（含跨文件挪入单条合法 MAC、重排条目）→ 打开即拒 | ✅ | `chunkcrypt.py:225-234`；实验 E2b/E2d |
| I3 | 头部任何字段改动（含 total_size/chunk_count/nonce）→ 打开即拒 | ✅ | `chunkcrypt.py:219-223`；实验 E3f |
| I4 | 文件长度必须恰好 = 头部声明的 `data_offset + total_size`，截断/追加都在打开时拒绝 | ✅ | `chunkcrypt.py:237-244`；实验 E3a/E3b/E3d/E3e/E3g |
| I5 | 被篡改的块在被读取时抛出 `ChunkTamperedError(chunk_index)`，可定位块号 | ✅ | `chunkcrypt.py:267-270`；实验 E1a/E2a/E3c |
| I6 | 篡改检测是**惰性**的：只校验实际读到的块；未被读的篡改块在打开时不报错 | ⚠️ 设计如此 | `chunkcrypt.py:260-271`；实验 E1a（open 成功、读块 2 才报错） |
| I7 | 所有 MAC 比较用 `hmac.compare_digest`（恒定时间） | ✅ | `chunkcrypt.py:221,232,269` |
| I8 | 三个 MAC 域（header/index/chunk）互不绑定：`chunk_mac`/`index_mac` 的输入**不含 nonce**，`header_mac` 不覆盖索引与数据 | ❌ 导致 E2c | `chunkcrypt.py:110-112, 156, 172` |
| I9 | 文件无 ID/版本/代际标识：同密钥、同 `chunk_size`、同 `total_size` 的两份密文的「头部」与「索引+数据」可自由拼接且全部校验通过 | ❌ 静默 | 实验 E2c |
| I10 | 防重放（旧版本整文件回滚） | ❌ 静默 | 实验 E2e；README「已知限制」已自认 |

### 2.3 块索引与随机访问

| # | 不变量 | 状态 | 证据 |
|---|--------|------|------|
| R1 | 随机读只触碰覆盖到的块：`first..last = offset//cs .. (offset+len-1)//cs` | ✅ | `read_at` `chunkcrypt.py:287-302` |
| R2 | 块内切片只生成所需密钥流（计数器从 `offset_in_chunk//32` 起），但 MAC 仍需读整块密文（读放大） | ✅ | `chunkcrypt.py:95-96, 280-285` |
| R3 | 越界读（负偏移、超过 total_size）拒绝 | ✅ | `chunkcrypt.py:289-290` |
| R4 | 尾块长度按 `total_size - i*chunk_size` 计算，支持非整块尾部 | ✅ | `_chunk_bounds` `chunkcrypt.py:255-258` |
| R5 | 空文件（0 块）合法：仅头部 + 空索引的 MAC | ✅ | `chunkcrypt.py:146, 158-160` |
| R6 | 打开时一次性把整个索引读入内存（每块 32B），随机读不再访问索引区 | ✅ | `chunkcrypt.py:225-235` |

## 3. 攻击实验结果（`experiments/run_attacks.py`，16 项）

| 实验 | 攻击 | 结果 | 检出点 |
|------|------|------|--------|
| E0 | 无攻击基线 | 正常往返一致 | — |
| E1a | 翻转数据区某块 1 字节 | **检出**：open 通过，读该块 `ChunkTamperedError(2)`，其它块正常 | 读取时，`chunkcrypt.py:269` |
| E1b | 用另一文件同位置“合法密文块”整块替换 | **检出**：`ChunkTamperedError(2)`（密文不同→MAC 失配） | 读取时 |
| E2a | 同文件内交换两个数据块（不动索引） | **检出**：块 0 正常，块 1/2 各报 `ChunkTamperedError(1)/(2)` | 读取时（块号绑定） |
| E2b | 只交换索引区两条 MAC | **检出**：打开即 `IntegrityError`（index_mac 失配） | 打开时，`chunkcrypt.py:232` |
| **E2c** | **跨文件载荷重放/嫁接**：同密钥同尺寸文件 B 的头部 + 文件 A 的索引区+数据区 | **静默接受**：打开、index_mac、逐块 MAC 全部通过，`read_all` 返回 320B 错乱明文，无任何异常 | 无（I8/I9 不成立） |
| E2d | 把 B 的单条 MAC 项挪进 A 的索引 | **检出**：打开即 `IntegrityError` | 打开时 |
| **E2e** | **整文件版本回滚**：用旧版本 v1 密文整体替换当前 v2 | **静默接受**：读回 v1 旧数据 | 无（无文件 ID/版本） |
| E3a | 砍掉最后半个块 | **检出**：打开即 `ChunkMissingError(4)` | 打开时，`chunkcrypt.py:240-242` |
| E3b | 中间挖掉一整块（文件变短） | **检出**：打开即 `ChunkMissingError(4)`（长度校验先于逐块） | 打开时 |
| E3c | 中间挖一块再用后续内容补齐到原长度 | **检出**：读错位块 `ChunkTamperedError(3)` | 读取时 |
| E3d | 截断到头部+部分索引 | **检出**：打开即 `IntegrityError("index region truncated")` | 打开时，`chunkcrypt.py:228-229` |
| E3e | 截断到不足一个头部 | **检出**：`ChunkCryptError("file too small")` | 打开时，`chunkcrypt.py:205-206` |
| E3f | 篡改头部 total_size 1 字节 | **检出**：打开即 `IntegrityError`（header_mac 失配） | 打开时 |
| E3g | 文件尾追加 16B 垃圾 | **检出**：打开即 `IntegrityError("trailing garbage")` | 打开时，`chunkcrypt.py:243-244` |
| E3h | 错误口令 | **检出**：打开即 `IntegrityError`（无法与篡改区分，属正常） | 打开时 |

**结论汇总**：16 项中 14 项被检出；2 项静默接受 —— **E2c（跨文件载荷嫁接）** 与 **E2e（整文件版本回滚）**。

## 4. 两个静默接受场景的根因

### 4.1 E2c 跨文件载荷嫁接（README 未记载，超出其“已知限制”）

三个 MAC 的认证域互不交叉：

- `chunk_mac` 输入只有 `"chunk"‖i‖ciphertext`（`chunkcrypt.py:110-112`）——**不含 nonce**；
- `index_mac` 输入只有 `"index"‖index`（`chunkcrypt.py:172`）——**不含 nonce/头部**；
- `header_mac` 只覆盖头部自身（`chunkcrypt.py:156`）——**不覆盖索引与数据**。

因此对**同一主密钥**、同 `chunk_size`、同 `total_size` 的任意两份密文，攻击者（仅有磁盘读写能力、无密钥）
可以把「文件 B 的头部」与「文件 A 的索引区+数据区」拼接成新文件：`header_mac` 用的是 B 的原值、
`index_mac` 用的是 A 的原值、逐块 MAC 与 A 的密文自洽——三层校验全部通过，但解密时用 B 的 nonce
生成密钥流去解 A 的密文，输出攻击者可控组合的**错乱明文**，且全程无任何异常。
这直接打破了「篡改可检出」的承诺：篡改后的文件被当作合法文件接受并返回数据。
前提“同密钥”在真实部署中常见（同一服务密钥加密多份落盘文件）。

### 4.2 E2e 整文件版本回滚

格式中没有文件 ID、版本号或单调计数器（头部字段见 `chunkcrypt.py:51-52`），
旧版本密文是完整自洽的合法文件，整体替换当前文件后所有校验自然通过。
README「已知限制」已自认此点，并建议“头部加入文件 ID/版本并外部留痕”。

### 修复方向（不在本次改动范围内）

把 `nonce`（或显式文件 ID/版本）纳入 `chunk_mac` 与 `index_mac` 的认证域，
例如 `HMAC(mac_key, "chunk"‖nonce‖i‖ct)`、`HMAC(mac_key, "index"‖nonce‖index)`，
即可同时封死 E2c 的拼接面；E2e 需另加版本/代际机制并外部锚定。

## 5. 其它观察（非安全结论）

- README 称头部 120B，代码实际 `HEADER_SIZE = 96`（`chunkcrypt.py:52`，实验脚本启动时打印核实）——文档笔误。
- 截断报错中的块号计算 `missing = (actual - data_offset) // chunk_size`（`chunkcrypt.py:241`）
  在“截断点落在块中间”时给出的是第一个不完整块的块号，语义正确。
- 惰性校验（I6）意味着“打开成功”不代表文件完好；调用方若只 open 不读，篡改不可见。
- 错口令与文件损坏同样表现为 `IntegrityError`（`chunkcrypt.py:222-223`），无法区分——属常见取舍。
- 口令加密的 KDF 迭代数取自文件头且代码不设下限（`chunkcrypt.py:132, 149`）；
  头部有 MAC 保护，外部攻击者无法利用，但合法调用方可自设弱参数（配置层面风险）。

## 6. 复现

```bash
cd /home/administrator/gsb/uid100/A
python3 experiments/run_attacks.py        # 期望退出码 0；输出与 experiments/RESULTS.txt 一致
ls experiments/tmp/                        # 查看各攻击构造的篡改样本
```

被测代码哈希校验：`git hash-object target/chunkcrypt.py` 应输出
`39e88abcf6626657b50c0b7f4839317dd12414af`。
