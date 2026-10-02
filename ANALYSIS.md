# chunkcrypt 加密文件格式：不变量审计与攻击实验报告

## 0. 范围与对象

- 被测代码：`/home/administrator/gsb/uid56/B/chunkcrypt.py`（316 行，Python 3，仅标准库）。
  当前工作目录 `/home/administrator/gsb/uid100/B` 中只有占位 `README.md`，
  与任务描述（分块加密 + 随机读取 + 逐块 HMAC）匹配的实现位于上述路径；
  审计全程**只读引用**，未对该文件做任何修改（实验脚本通过 `importlib` 动态导入）。
- 实验脚本：`analysis/attack_experiments.py`（仅标准库）
- 一次完整运行的输出存档：`analysis/results.txt`
- 运行环境：Python 3.12.3 / Linux。

## 1. 格式与密钥体系速览（依据 `chunkcrypt.py`）

文件布局（实测偏移，注意 README 里写的「头部 120B」是笔误，代码实际为 96B）：

| 区域 | 偏移 | 内容 |
| --- | --- | --- |
| 头部 | 0 | `magic(8) chunk_size(4) total_size(8) chunk_count(8) kdf_iter(4) salt(16) nonce(16) header_mac(32)`，共 **96B**（`L51-52`） |
| 索引区 | 96 | `chunk_count` 个 32B 的块 MAC，随后 32B `index_mac` |
| 数据区 | `96 + chunk_count*32 + 32` | 与明文等长的密文块 |

- 密钥派生：`enc_key/mac_key = HMAC(master, "chunkcrypt/enc"|"chunkcrypt/mac")`（`L85-88`）；
  口令路径为 PBKDF2-HMAC-SHA256（`L125-126`）。
- 加密：PRF-CTR 流密码，密钥流块 `SHA256(enc_key || nonce || chunk_index || counter)`（`L91-101`）。
- 完整性：`chunk_mac = HMAC(mac_key, "chunk" || chunk_index || ciphertext)`（`L110-112`），
  `header_mac`（`L156`）、`index_mac`（`L172`），比较用 `hmac.compare_digest`（`L221`, `L232`, `L269`）。
- 打开流程 `ChunkReader._load`：读头 → 校验 magic → **派生密钥** → 校验 header_mac →
  读索引 → 校验 index_mac → 用 `total_size` 比对实际文件长度（`L203-244`）。
- 随机读 `read_at`：算出覆盖块号，每块**先读整块密文校验 MAC，再只为所需切片生成密钥流**（`L260-302`）。

## 2. 不变量清单（✅ 代码强制 / ❌ 实际不成立 / ⚠️ 仅概率保证）

### 保密性与加密

| # | 不变量 | 状态 | 证据 |
| --- | --- | --- | --- |
| C1 | 密文与明文等长（流密码），最后一块按 `total_size` 取真实长度 | ✅ | `_xor`/`_keystream` `L91-107`；`_chunk_bounds` `L255-258` |
| C2 | 每文件 16B 随机 nonce，块号+计数器参与密钥流，块内/跨块密钥流不重复 | ⚠️ | `os.urandom` `L152`；`L97-99`。无持久化防重放，仅以 2^128 随机空间概率保证 nonce 不撞 |
| C3 | 主密钥经 HMAC 域分离，enc/mac 密钥相互独立 | ✅ | `L85-88` |

### 完整性校验

| # | 不变量（文档宣称） | 状态 | 证据与实验 |
| --- | --- | --- | --- |
| I1 | 改动密文任意字节可检出，并能定位到块号 | ✅ | MAC 输入含块号 `L110-112`；校验 `L267-270`；E1/E2 |
| I2 | 篡改只影响本块，其它块仍可读（逐块独立） | ✅ | 每读一块校验一块 `L260-271`；E1 局部性观察 |
| I3 | 整块替换（任意块号）可检出 | ✅ | 位置绑定 `L111`；E2 |
| I4 | 块重排可检出——数据区单独交换被块号绑定拦住，连索引项一起交换被 index_mac 拦住 | ✅ | `L111` + `L230-234`；E3/E4 |
| I5 | 索引项改动（删/改/重排）打开即拒绝 | ✅ | index_mac `L230-234`；E4/E7/E12 |
| I6 | 头部元数据改动 / 错密钥打开即拒绝 | ✅ | header_mac `L219-223` |
| I7 | 数据区截断/删块可检出 | ✅ | 打开时长度比对 `L238-242`（整块缺失）；索引区截断 `L228-229`；头部过短 `L205-206`；E5 |
| I8 | 中间挖块并补字节（长度不变）可检出 | ✅ | 错位后块号 MAC 失配 `L267-270`；E5b |
| I9 | 尾部追加 / 两文件拼接可检出 | ✅ | trailing garbage 检查 `L243-244`；E6 |
| I10 | 跨文件/跨版本**单块**重放（密文+索引项一起搬）可检出 | ✅ | index_mac 绑定整表 `L230-234`；E7、E12 |
| I11 | **整文件重放（旧版本整体覆盖）可检出** | ❌ | 全格式无文件 ID/版本/新鲜度字段；v1 文件本身所有 MAC 均合法。E9 **静默接受并返回旧明文**（README「已知限制」一节自己也承认） |
| I12 | 头部、索引、数据三段相互绑定，**跨文件拼接**应拒绝 | ❌ | header_mac 只覆盖头部 `L156`，index_mac 只覆盖索引 `L172`，chunk_mac 不含 nonce `L110-112`——三段之间没有任何绑定 MAC。E8/E10：A 头 + B 身（同密钥同长度）**打开与读取全程无异常，返回谁都没写过的乱码**（静默的完整性破坏） |
| I13 | 打开一个文件不会因攻击者构造的字段而付出高额 CPU 代价 | ❌ | `_resolve_key`（PBKDF2）在 `L216` 先于 header_mac 校验（`L219`）执行，而 `kdf_iterations` 来自尚未认证的头部（u32，最大 2^32-1）。E11：改成 3e7 轮后，明知 MAC 必失配仍先做 4.4s PBKDF2 才报 IntegrityError → 打开期 CPU-DoS |

### 块索引与随机访问

| # | 不变量 | 状态 | 证据 |
| --- | --- | --- | --- |
| R1 | 块号 i 与磁盘偏移严格对应：`data_offset + i*chunk_size` | ✅ | `L255-263` |
| R2 | 块 MAC 把逻辑块号纳入输入，「物理位置即逻辑位置」不可被交换 | ✅ | `L111`；E3 |
| R3 | 随机读只触碰覆盖块，且返回前必过该块 MAC（不存在「跳过校验的快速读」） | ✅ | `read_at` `L287-302` → `_read_chunk_slice` `L280-285` → `_read_cipher_chunk` `L260-271` |
| R4 | 读取越界（越 total_size / 负参数）拒绝 | ✅ | `L289-290` |
| R5 | 读 4KB 也要把整块（默认 1MiB）从磁盘读入（读放大） | ⚠️ 设计取舍 | `L264` 读整块；README 已说明 |
| R6 | 被截断文件打开即失败，无需等到读到尾部才发现 | ✅ | 打开时全长度比对 `L238-242`；E5 |
| R7 | 篡改检测是懒触发：不读的块不校验（打开期不扫全文件） | ⚠️ 设计取舍 | 打开只校验头/索引/长度 `L203-244`；块 MAC 仅在读时 `L267-270`。未读块的篡改不会被主动发现 |

## 3. 攻击实验结果汇总

脚本 `analysis/attack_experiments.py`，`--src` 指定被测目录，默认
`/home/administrator/gsb/uid56/B`；实验文件全部建在临时目录，被测代码零改动。

| 实验 | 攻击 | 结果 | 异常/观察 |
| --- | --- | --- | --- |
| E0 | 基线往返 | 正常 | 明文逐字节一致 |
| E1 | chunk0 翻转 1 bit | ✅ 检出 | `ChunkTamperedError(0)`；chunk2 仍可读 |
| E2 | chunk1 整块随机替换 | ✅ 检出 | `ChunkTamperedError(1)` |
| E3 | 交换 chunk0/1 密文（数据区） | ✅ 检出 | `ChunkTamperedError(0)`（块号绑定） |
| E4 | 数据 + 索引项同步交换 | ✅ 检出 | `IntegrityError`（index_mac 失配，打开即拒） |
| E5 | 尾部截短 32B（块截断） | ✅ 检出 | `ChunkMissingError(3)`（打开即拒） |
| E5b | 中间挖一块+末尾补零（长度不变） | ✅ 检出 | `ChunkTamperedError(1)`（后续错位） |
| E6 | 尾部追加 32B | ✅ 检出 | `IntegrityError: trailing garbage` |
| E7 | 跨文件单块重放（密文+索引项） | ✅ 检出 | `IntegrityError`（index_mac 失配） |
| **E8** | **A 头 + B 索引/数据（同密钥同长度）** | ❌ **静默接受** | 无任何异常，读出 256B 乱码，既不等于 A 原文也不等于 B 原文 |
| **E9** | **旧版本密文整体覆盖新文件（回滚重放）** | ❌ **静默接受** | 无任何异常，明文开头为 `b'OLD-SECRET-v1'` |
| **E10** | **新头 + 旧索引/旧数据（部分回滚）** | ❌ **静默接受** | 同 E8，三段无绑定，返回乱码 |
| **E11** | 头部 `kdf_iterations` 改 3e7（不重算 MAC） | ❌ 先干活后报错 | 4.47s PBKDF2 后才 `IntegrityError`（正常 200k 轮约 0.03s） |
| E12 | 同文件旧版本单块回滚（密文+索引项） | ✅ 检出 | `IntegrityError`（index_mac 失配） |

一句话结论：**块内篡改、整块替换、重排、截断、追加、单块重放都被正确强制；
缺口集中在「跨版本/跨文件的整体语义」——整文件回滚（E9）与三段拼接（E8/E10）
被静默接受，另外打开流程把未认证的 PBKDF2 迭代数当可信值（E11，CPU-DoS）。**

## 4. 根因分析

1. **无新鲜度锚点（E9）**。格式中没有文件 ID、版本号、单调计数器或时间戳，
   MAC 只回答「这份字节是不是在该密钥下由合法方写过」。旧版本密文对该命题同样为真，
   所以回滚在密码学上无法与合法文件区分。这不是编码疏漏，而是缺一层设计；
   README「已知限制」也承认了这一点，但没有任何运行期告警。
2. **三段认证域互不相交（E8/E10）**。`header_mac=H("header"||H)`、
   `index_mac=H("index"||I)`、`chunk_mac=H("chunk"||i||C_i)`（`L110-112`, `L156`, `L172`）。
   且块 MAC 与 nonce 无关，因此「A 的头（含 A 的 nonce/长度）+ B 的索引与数据」
   能同时通过三层校验。后果是：两个同密钥、同 `chunk_size/total_size` 的文件之间
   互换主体，读者拿到的是 `B密文 XOR A密钥流` 的乱码，**完整性校验形同通过**。
   攻击者不能借此读出有意义的旧明文（要旧明文需整体回滚，即 E9），但可以制造
   「校验通过、数据已毁」的状态，用于破坏依赖「校验通过即可信」的上层逻辑。
3. **先 KDF 后认证（E11）**。`L216` 用头部里的 `iterations` 做 PBKDF2，
   而头部 MAC 到 `L219` 才检查。攻击者给一个口令模式文件塞入 2^32-1 轮迭代数，
   接收方在拒绝前就被迫做巨量哈希。正确顺序应是先用固定/上限迭代数（或先校验
   header_mac——但口令文件在 KDF 前没有 mac_key，实际做法是把迭代数钳制在
   协商上限内，例如拒绝 `>1_000_000`）。

## 5. 修复建议（不在本次改动范围内，仅作结论）

- 防回滚：头部加入随机 `file_id` + 单调 `version`，并在外部可信存储
  （或头/尾部对「前一版本摘要」做链式锚定）中记录最新版本；读者校验版本单调。
- 绑定三段：把 nonce/file_id 纳入 chunk_mac 输入，并在头部放
  `H("root" || header || index_mac || 数据区摘要)` 式的单一根 MAC（或 AEAD 化）。
- E11：KDF 前钳制 `kdf_iterations` 到配置上限；或头部 MAC 用口令的独立
  低速派生密钥先验证再正式 KDF。
- 打开时可选 `verify_all` 全量扫块，弥补懒校验（R7）下未读块篡改不可见的取舍。

## 6. 复现方式

```bash
# 攻击实验（只读，不修改 chunkcrypt.py；仅用 Python 3 标准库）
python3 /home/administrator/gsb/uid100/B/analysis/attack_experiments.py \
  --src /home/administrator/gsb/uid56/B

# 对照：被测实现自带的 19 项功能自测
cd /home/administrator/gsb/uid56/B && python3 -m unittest -v test_chunkcrypt.py
```

- 脚本每次运行在 `/tmp/chunkcrypt-atk-*` 下生成 256B（4 块，块长 64B）的最小样本，
  按字节构造 E1–E12 的篡改；输出中 `[检出]` = 抛出异常，`[静默接受]` = 无异常返回数据。
- `analysis/results.txt` 是一次完整运行的实际输出存档（含异常消息与耗时）。
