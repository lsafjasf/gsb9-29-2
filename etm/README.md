# 认证加密封装（Encrypt-then-MAC）

仅用 Node.js 标准库 `crypto`，实现 **AES-256-CTR（加密） + HMAC-SHA256（认证）** 的
先加密后认证（EtM）封装，并提供无依赖自测。

- 运行时：Node.js >= 16（在 v18.19.1 上验证）
- 第三方依赖：无

## 运行方式

```bash
cd etm
node example.js      # 最小可用示例
node etm.test.js     # 自测（篡改矩阵 + 边界），全通过时退出码 0
# 或
npm test
```

## 目录

- `etm.js`：核心库（seal/open、HKDF 派生、错误分类）
- `etm.test.js`：自测，32 个用例
- `example.js`：最小示例
- `README.md`：本说明（含组合原理与边界说明）

## API

```js
const { createCodec, randomKey, EtmError } = require('./etm');

const codec = createCodec(randomKey());          // 主密钥 >=16B，建议 32B 随机
const pkt = codec.seal(plaintextBuf, aadBuf, keyId); // keyId 可省，默认 0
const pt  = codec.open(pkt, aadBuf);             // 验签通过才解密；否则抛 EtmError
```

- `plaintext`、`aad`、`packet` 都必须是 `Buffer`；`aad` 可省略（按空处理）。
- 错误对象为 `EtmError`，用 `err.code` 区分类型：

| code | 含义 |
| --- | --- |
| `EINPUT` | 入参类型/长度非法（封装前拦截） |
| `ETOOLARGE` | 明文超过 u32 长度前缀上限（4,294,967,295 字节） |
| `ETRUNC` | 数据包物理截断，短于最小长度（49 字节）或长度无法容纳标签 |
| `EBADHDR` | 头部版本/算法套件字段为不支持的值 |
| `EAUTH` | MAC 校验失败：密文、nonce、keyId、AAD 被改，或字段重排、（非物理）截断、扩展 |

> 验签失败对调用方统一返回 `EAUTH`，不区分“到底动了哪里”，避免差异信息被利用；
> 但每个自测用例都明确标注了攻击类型，可对照检出结果。

## 数据包格式

```
header(5) || nonce(12) || ciphertext(N) || tag(32)

header     = version(1)=0x01 | suite(1)=0x02 | keyId(3, big-endian)
nonce      = 每次封装由 CSPRNG 生成的 12 字节随机数
ciphertext = AES-256-CTR(encKey, nonce(12)||0x00000000, plaintext)
tag        = HMAC-SHA256(macKey, MAC_input)
```

`MAC_input` 为**带 u32 长度前缀、字段顺序固定**的规范编码：

```
u32(len(header))     || header
u32(len(nonce))      || nonce
u32(len(ciphertext)) || ciphertext
u32(len(aad))        || aad
```

密钥从主密钥经 HKDF-SHA256 派生出彼此独立的 `encKey` 与 `macKey`
（`info` 分别为 `"enc"||keyId`、`"mac"||keyId`，salt 固定为协议常量）。

## 组合说明：为什么必须先加密、后认证

本实现采用 **Encrypt-then-MAC（EtM）**：先算出密文，再对密文连同头部/nonce/AAD 一起做 MAC。

- **EtM：验签在解密之前。** 只有 MAC 合法的包才会被送进解密器。攻击者任何位级改动
  （密文、nonce、头部、标签、AAD、长度）都会先在 HMAC 这一步被拒，解密器永远不会
  处理攻击者构造的字节。EtM 是可证明安全的通用构造，也是 IPsec/SSHv2 等采用的顺序。
- **不能颠倒成 Encrypt-and-MAC（E&M，各签各的）**：MAC 不保护密文完整性，
  解密可能先于/独立于验签发生，存在用解密结果做反馈的选择密文攻击面（MAC 本身还可能泄露明文信息）。
- **也不能用 MAC-then-Encrypt（MtE，先把“明文+标签”一起加密）**：
  验证必须先“解密再比对填充/标签”，历史上的 padding oracle（如 SSL/TLS 的 BEAST/Lucky Thirteen
  一类问题）正是利用这一步返回的差异；而且标签覆盖的是明文而非线上实际传输的字节，
  头部/AAD 若不额外处理就容易漏保护。

一句话：顺序反了，验证就发生在“接触了攻击者数据”之后，会留下侧信道与选择密文攻击面。

## 为什么长度前缀能防字段调换/边界挪动

单纯 `HMAC(key, A||B||C)` 时，接收方若按可变字段切分，不同的切分可能得到同一拼接串：

```
("ab","c") 与 ("a","bc") 的 concat 都是 "abc"
```

这使攻击者可以在字段间挪动字节、调换字段边界而不改变“被认证的拼接结果”。
本实现对每个字段都加 `u32` 长度前缀且顺序固定，字段划分是**唯一**的：

- 上面两组的规范编码因长度前缀不同而不同，HMAC 必然不同 -> 拒绝；
- 字段整体调换顺序也会改变 `MAC_input` -> 拒绝（自测第三节直接断言）。

## 篡改用例与检出结果（自测实际输出）

`node etm.test.js` 的结果为 **32 通过 / 0 失败**，覆盖：

- 密文：翻转首/中/末字节、末块字节 → `EAUTH`
- 标签：翻转 1 字节、整体替换 32 字节 → `EAUTH`
- 头部：`keyId` 翻转 → `EAUTH`；version/suite 改成未知值 → `EBADHDR`
- nonce：翻转 1 字节（计数器/重放材料被改）→ `EAUTH`
- 字段重排：头部字节与密文字节互换、密文内部字节互换 → `EAUTH`
- 截断：短于 49 字节、丢失整个标签 → `ETRUNC`；砍掉标签 1 字节/密文若干字节 → `EAUTH`
- AAD：换用不同关联数据或缺省 → `EAUTH`
- 扩展/伪造：尾部追加垃圾、合法头+随机体、完全随机包 → `EAUTH`/`EBADHDR`，全部拒绝

## 边界用例

- **空明文**：包长恰为 49 字节，仍因随机 nonce 每次不同，可正常往返。
- **空 AAD**：与非空 AAD 互不兼容，混淆即 `EAUTH`。
- **超长明文**：1 MiB 随机数据往返；超过 u32 上限在 `seal` 阶段抛 `ETOOLARGE`。
  单条 CTR 消息理论上限为 2^32 个分组（约 64 GiB，计数器 32 位），本实现取更保守的 u32 字节上限。
- **随机数源被固定**：`createCodec(key, { randomBytes })` 仅为测试钩子。
  - 固定后同一明文封装结果完全一致、可复现，且仍能正确验签解密；
  - 自测同时断言了 CTR 的固有事实：**nonce 复用会令两条密文的异或等于两条明文的异或**，
    故生产环境绝不允许固定/复用 nonce（每次都用默认的 `crypto.randomBytes(12)`）。
- **错误主密钥**：`EAUTH`，不会解出错误明文。
- **随机化**：默认同一明文两次封装密文不同，解密结果一致（另含 200 组随机往返交叉验证）。

## 安全注意

- 主密钥必须高熵、妥善托管；不要把主密钥与数据包存放在同一信任域。
- 不要复用 nonce；`randomBytes` 注入钩子仅供确定性测试。
- 本方案不内置重放防护（无状态）。若需防重放，可把单调序号/时间戳放进 `aad`，
  由调用方维护接收窗口去重。
- 需要跨语言互通时，按上文“数据包格式”实现即可；CTR 起始块为 `nonce||0x00000000`。
