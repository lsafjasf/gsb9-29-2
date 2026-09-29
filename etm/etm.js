'use strict';

// 认证加密封装：AES-256-CTR（加密） + HMAC-SHA256（认证），先加密后认证 (Encrypt-then-MAC)。
// 仅使用 Node.js 标准库 crypto。
//
// 数据包布局（均为定长字段，无歧义解析）：
//
//   header(5) || nonce(12) || ciphertext(N) || tag(32)
//
//   header: version(1)=0x01 | suite(1)=0x02(AES-256-CTR/HMAC-SHA256) | keyId(3, big-endian)
//   nonce : 每次封装随机生成的 12 字节；CTR 起始计数器块 = nonce(12) || 0x00000000(4)
//   tag   : HMAC-SHA256(macKey, macInput)，macInput 为带长度前缀的规范编码：
//             u32(header.length)      || header
//             u32(nonce.length)       || nonce
//             u32(ciphertext.length)  || ciphertext
//             u32(aad.length)         || aad
//           header、nonce、ciphertext、aad 全部在认证范围内。
//
// 密钥：由主密钥经 HKDF-SHA256 派生出彼此独立的 encKey / macKey（info 绑定 keyId）。

const crypto = require('crypto');

const VERSION = 0x01;
const SUITE_AES256CTR_HMACSHA256 = 0x02;
const HEADER_LEN = 5;
const NONCE_LEN = 12;
const TAG_LEN = 32;
const KEY_LEN = 32;
const MIN_PACKET_LEN = HEADER_LEN + NONCE_LEN + TAG_LEN; // 49
const MAX_PLAINTEXT_LEN = 0xffffffff; // 长度前缀为 u32

class EtmError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'EtmError';
    this.code = code; // EINPUT | ETRUNC | EBADHDR | EAUTH | ETOOLARGE
  }
}

function u32be(n) {
  const b = Buffer.alloc(4);
  b.writeUInt32BE(n >>> 0, 0);
  return b;
}

function hmac(key, data) {
  return crypto.createHmac('sha256', key).update(data).digest();
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

// HKDF-SHA256（RFC 5869），手写以保证可审计。
function hkdf(ikm, salt, info, length) {
  const prk = hmac(salt, ikm);
  const parts = [];
  let previous = Buffer.alloc(0);
  let produced = 0;
  let counter = 1;
  while (produced < length) {
    previous = hmac(prk, Buffer.concat([
      previous,
      info,
      Buffer.from([counter]),
    ]));
    parts.push(previous);
    produced += previous.length;
    counter += 1;
  }
  return Buffer.concat(parts).subarray(0, length);
}

function deriveKeys(masterKey, keyIdBuf) {
  if (!Buffer.isBuffer(masterKey) || masterKey.length < 16) {
    throw new EtmError('EINPUT', 'masterKey 必须是长度 >= 16 字节的 Buffer（建议 32 字节随机）');
  }
  const salt = Buffer.from('ETM-v1/AES-256-CTR/HMAC-SHA256', 'utf8');
  const encKey = hkdf(masterKey, salt, Buffer.concat([Buffer.from('enc', 'utf8'), keyIdBuf]), KEY_LEN);
  const macKey = hkdf(masterKey, salt, Buffer.concat([Buffer.from('mac', 'utf8'), keyIdBuf]), KEY_LEN);
  return { encKey, macKey };
}

function buildHeader(keyId) {
  if (!Number.isInteger(keyId) || keyId < 0 || keyId > 0xffffff) {
    throw new EtmError('EINPUT', 'keyId 必须是 0..2^24-1 的整数');
  }
  const header = Buffer.alloc(HEADER_LEN);
  header[0] = VERSION;
  header[1] = SUITE_AES256CTR_HMACSHA256;
  header.writeUIntBE(keyId, 2, 3);
  return header;
}

// 认证输入：所有字段都带 u32 长度前缀，字段顺序固定。
// 长度前缀使 (A,B) 与 (A',B') 不可能在拼接后碰撞 -> 防止字段调换/混淆。
function buildMacInput(header, nonce, ciphertext, aad) {
  return Buffer.concat([
    u32be(header.length), header,
    u32be(nonce.length), nonce,
    u32be(ciphertext.length), ciphertext,
    u32be(aad.length), aad,
  ]);
}

function asBuffer(value, name, allowEmpty) {
  if (value === undefined || value === null) {
    if (allowEmpty) return Buffer.alloc(0);
    throw new EtmError('EINPUT', `${name} 必须是 Buffer`);
  }
  if (!Buffer.isBuffer(value)) {
    throw new EtmError('EINPUT', `${name} 必须是 Buffer`);
  }
  return value;
}

// 生成 32 字节随机主密钥。
function randomKey() {
  return crypto.randomBytes(KEY_LEN);
}

// 创建一个 codec。options.randomBytes 仅供测试（固定随机数源），生产环境不要注入。
function createCodec(masterKey, options) {
  const opts = options || {};
  const randomBytes = opts.randomBytes || ((n) => crypto.randomBytes(n));

  function seal(plaintext, aad, keyId) {
    const pt = asBuffer(plaintext, 'plaintext', true);
    const ad = asBuffer(aad, 'aad', true);
    const kid = keyId === undefined ? 0 : keyId;
    if (pt.length > MAX_PLAINTEXT_LEN) {
      throw new EtmError('ETOOLARGE', `明文过长：${pt.length} > ${MAX_PLAINTEXT_LEN}`);
    }
    const header = buildHeader(kid);
    const keyIdBuf = header.subarray(2, 5);
    const { encKey, macKey } = deriveKeys(masterKey, keyIdBuf);

    const nonce = randomBytes(NONCE_LEN);
    if (!Buffer.isBuffer(nonce) || nonce.length !== NONCE_LEN) {
      throw new EtmError('EINPUT', 'randomBytes 必须返回 12 字节 Buffer');
    }
    const iv = Buffer.concat([nonce, Buffer.alloc(4)]); // 16 字节，计数器初值 0

    const cipher = crypto.createCipheriv('aes-256-ctr', encKey, iv);
    const ciphertext = Buffer.concat([cipher.update(pt), cipher.final()]);

    const tag = hmac(macKey, buildMacInput(header, nonce, ciphertext, ad));
    return Buffer.concat([header, nonce, ciphertext, tag]);
  }

  // 返回明文 Buffer；任何结构损坏或认证失败都抛出 EtmError，绝不返回可疑明文。
  function open(packet, aad) {
    const pkt = asBuffer(packet, 'packet', false);
    const ad = asBuffer(aad, 'aad', true);

    if (pkt.length < MIN_PACKET_LEN) {
      throw new EtmError('ETRUNC', `数据包被截断：长度 ${pkt.length} < 最小长度 ${MIN_PACKET_LEN}`);
    }
    if (pkt[0] !== VERSION || pkt[1] !== SUITE_AES256CTR_HMACSHA256) {
      throw new EtmError('EBADHDR', `头部版本/算法套件不被支持：version=${pkt[0]} suite=${pkt[1]}`);
    }
    const header = pkt.subarray(0, HEADER_LEN);
    const keyIdBuf = header.subarray(2, 5);
    const nonce = pkt.subarray(HEADER_LEN, HEADER_LEN + NONCE_LEN);
    const tagOffset = pkt.length - TAG_LEN;
    const ciphertext = pkt.subarray(HEADER_LEN + NONCE_LEN, tagOffset);
    const tag = pkt.subarray(tagOffset);

    const { encKey, macKey } = deriveKeys(masterKey, keyIdBuf);
    const expectedTag = hmac(macKey, buildMacInput(header, nonce, ciphertext, ad));
    if (!timingSafeEqual(tag, expectedTag)) {
      // 不区分是密文/nonce/keyId/AAD 哪一处被改：统一拒绝，避免泄露可被利用的差异信息。
      throw new EtmError('EAUTH', '认证失败：数据包、头部或关联数据可能被篡改/截断/重排');
    }

    const iv = Buffer.concat([nonce, Buffer.alloc(4)]);
    const decipher = crypto.createDecipheriv('aes-256-ctr', encKey, iv);
    return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
  }

  return { seal, open };
}

module.exports = {
  createCodec,
  randomKey,
  EtmError,
  canonicalize: buildMacInput,
  constants: {
    VERSION,
    SUITE_AES256CTR_HMACSHA256,
    HEADER_LEN,
    NONCE_LEN,
    TAG_LEN,
    MIN_PACKET_LEN,
    MAX_PLAINTEXT_LEN,
  },
};
