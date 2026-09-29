'use strict';

// 自测：无外部测试框架，直接 `node etm.test.js`，失败时退出码非 0。
const assert = require('assert');
const crypto = require('crypto');
const {
  createCodec,
  randomKey,
  EtmError,
  canonicalize,
  constants,
} = require('./etm');

let passed = 0;
const failures = [];

function ok(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  PASS  ${name}`);
  } catch (err) {
    failures.push({ name, err });
    console.log(`  FAIL  ${name} -> ${err && err.stack ? err.stack.split('\n')[0] : err}`);
  }
}

function expectCode(fn, code) {
  let thrown = null;
  try {
    fn();
  } catch (err) {
    thrown = err;
  }
  assert.ok(thrown instanceof EtmError, `期望抛出 EtmError，实际：${thrown}`);
  assert.strictEqual(thrown.code, code, `期望 code=${code}，实际 code=${thrown.code}`);
}

// 用确定性方式修改某个字节：对指定偏移做 XOR。
function flipByte(buf, offset) {
  const out = Buffer.from(buf);
  out[offset] ^= 0x01;
  return out;
}

const key = randomKey();
const codec = createCodec(key);

console.log('一、功能正确性 / 随机化 / 边界');

ok('同一明文两次封装结果不同（随机 nonce），但解密一致', () => {
  const pt = Buffer.from('hello authenticated encryption', 'utf8');
  const p1 = codec.seal(pt);
  const p2 = codec.seal(pt);
  assert.ok(!p1.equals(p2), '两次封装不应相同（nonce 随机化）');
  assert.ok(codec.open(p1).equals(pt));
  assert.ok(codec.open(p2).equals(pt));
});

ok('关联数据 AAD 参与认证：相同密文、不同 AAD 必须拒绝', () => {
  const pt = Buffer.from('payload', 'utf8');
  const pkt = codec.seal(pt, Buffer.from('context-A', 'utf8'));
  assert.ok(codec.open(pkt, Buffer.from('context-A', 'utf8')).equals(pt));
  expectCode(() => codec.open(pkt, Buffer.from('context-B', 'utf8')), 'EAUTH');
  expectCode(() => codec.open(pkt), 'EAUTH'); // 缺省 AAD 也不匹配
});

ok('空明文：可往返且仍随机化', () => {
  const a = codec.seal(Buffer.alloc(0));
  const b = codec.seal(Buffer.alloc(0));
  assert.strictEqual(a.length, constants.MIN_PACKET_LEN);
  assert.ok(!a.equals(b));
  assert.strictEqual(codec.open(a).length, 0);
});

ok('空 AAD 与非空 AAD 不可互换', () => {
  const pt = Buffer.from('x', 'utf8');
  const pkt = codec.seal(pt, Buffer.alloc(0));
  assert.ok(codec.open(pkt, Buffer.alloc(0)).equals(pt));
  expectCode(() => codec.open(pkt, Buffer.from('y', 'utf8')), 'EAUTH');
});

ok('超长明文（1 MiB 随机数据）正确往返', () => {
  const pt = crypto.randomBytes(1024 * 1024);
  const pkt = codec.seal(pt, Buffer.from('big', 'utf8'));
  assert.ok(codec.open(pkt, Buffer.from('big', 'utf8')).equals(pt));
});

ok('超过 u32 长度上限在封装阶段报错（ETOOLARGE）', () => {
  const huge = Buffer.alloc(constants.MAX_PLAINTEXT_LEN + 1);
  expectCode(() => codec.seal(huge), 'ETOOLARGE');
});

ok('随机数源被固定：输出确定、可复现；且仍能正确解密', () => {
  const fixedNonce = Buffer.from('0123456789ab', 'utf8'); // 12 字节
  const cFixed = createCodec(key, { randomBytes: () => Buffer.from(fixedNonce) });
  const pt = Buffer.from('pinned-rng', 'utf8');
  const a = cFixed.seal(pt);
  const b = cFixed.seal(pt);
  assert.ok(a.equals(b), 'nonce 被固定后封装结果应完全一致（可复现）');
  assert.ok(codec.open(a).equals(pt), '固定 nonce 的包仍可被标准 codec 验证解密');
  // 告警性质的事实：固定 nonce + CTR 会令两条密文的“差”等于明文异或。
  const ptx = Buffer.from('AAAAAAAAAA', 'utf8');
  const pty = Buffer.from('BBBBBBBBBB', 'utf8');
  const cx = cFixed.seal(ptx).subarray(constants.HEADER_LEN + constants.NONCE_LEN, -constants.TAG_LEN);
  const cy = cFixed.seal(pty).subarray(constants.HEADER_LEN + constants.NONCE_LEN, -constants.TAG_LEN);
  assert.ok(cx.map((v, i) => v ^ cy[i]).equals(ptx.map((v, i) => v ^ pty[i])),
    '演示：nonce 复用时 CTR 密文异或即明文异或（故生产环境必须随机 nonce）');
});

ok('错误主密钥：认证必须失败（EAUTH），不会得到错误明文', () => {
  const pkt = codec.seal(Buffer.from('secret', 'utf8'));
  const other = createCodec(randomKey());
  expectCode(() => other.open(pkt), 'EAUTH');
});

ok('非 Buffer 入参报 EINPUT', () => {
  expectCode(() => codec.seal('not-a-buffer'), 'EINPUT');
  expectCode(() => codec.open('not-a-buffer'), 'EINPUT');
});

console.log('\n二、篡改用例矩阵（全部必须被检出并拒绝）');

const refPt = Buffer.from('bank-transfer: amount=100 to=alice', 'utf8');
const refAad = Buffer.from('session=42; sender=bob', 'utf8');
const ref = codec.seal(refPt, refAad, 7);
const H = constants.HEADER_LEN;
const N = constants.NONCE_LEN;
const T = constants.TAG_LEN;

function tamper(name, mutate, code, openAad) {
  ok(name, () => {
    const evil = mutate(Buffer.from(ref));
    expectCode(() => codec.open(evil, openAad === undefined ? refAad : openAad), code);
  });
}

// 1) 密文
tamper('密文：翻转中间 1 字节 -> EAUTH',
  (p) => flipByte(p, H + N + 5), 'EAUTH');
tamper('密文：翻转第 1 字节 -> EAUTH',
  (p) => flipByte(p, H + N), 'EAUTH');
tamper('密文：翻转最后 1 字节（密文末块）-> EAUTH',
  (p) => flipByte(p, p.length - T - 1), 'EAUTH');

// 2) 认证标签
tamper('标签：翻转 1 字节 -> EAUTH',
  (p) => flipByte(p, p.length - 1), 'EAUTH');
tamper('标签：整体替换为其它 32 字节 -> EAUTH',
  (p) => { const q = Buffer.from(p); crypto.randomBytes(T).copy(q, p.length - T); return q; }, 'EAUTH');

// 3) 头部（结构上仍合法，但内容被改 -> 认证失败）
tamper('头部：keyId 翻转 1 字节（密钥标识被改）-> EAUTH',
  (p) => flipByte(p, 2), 'EAUTH');
tamper('头部：版本号被改成 0x01 以外（结构损坏）-> EBADHDR',
  (p) => { const q = Buffer.from(p); q[0] = 0x09; return q; }, 'EBADHDR');
tamper('头部：算法套件被改成未知值（结构损坏）-> EBADHDR',
  (p) => { const q = Buffer.from(p); q[1] = 0xFF; return q; }, 'EBADHDR');

// 4) nonce（重放/重排计数器）
tamper('随机数 nonce：翻转 1 字节 -> EAUTH',
  (p) => flipByte(p, H), 'EAUTH');

// 5) 字段重排 / 跨字段调换
tamper('字段重排：交换头部 keyId 字节与密文首字节（跨字段调换）-> EAUTH',
  (p) => {
    const q = Buffer.from(p);
    const i = 2;          // 头部内
    const j = H + N;      // 密文第 1 字节
    const tmp = q[i]; q[i] = q[j]; q[j] = tmp;
    return q;
  }, 'EAUTH');
tamper('字段重排：交换密文内部两个字节（重排密文）-> EAUTH',
  (p) => {
    const q = Buffer.from(p);
    const i = H + N;
    const j = H + N + 6;
    const tmp = q[i]; q[i] = q[j]; q[j] = tmp;
    return q;
  }, 'EAUTH');

// 6) 截断
tamper('截断：整包仅 10 字节（短于最小长度）-> ETRUNC',
  (p) => p.subarray(0, 10), 'ETRUNC');
tamper('截断：恰好丢失整个标签（留 header+nonce）-> ETRUNC',
  (p) => p.subarray(0, H + N), 'ETRUNC');
tamper('截断：仅砍掉标签最后 1 字节 -> EAUTH',
  (p) => p.subarray(0, p.length - 1), 'EAUTH');
tamper('截断：砍掉密文尾部若干字节（标签仍保留但对不上）-> EAUTH',
  (p) => Buffer.concat([p.subarray(0, p.length - T - 4), p.subarray(p.length - T)]), 'EAUTH');

// 7) 关联数据
tamper('AAD：解密时换用不同关联数据 -> EAUTH',
  (p) => p, 'EAUTH', Buffer.from('session=42; sender=mallory', 'utf8'));

// 8) 随机扩展 / 伪造
tamper('扩展：在标签之后附加垃圾字节 -> EAUTH',
  (p) => Buffer.concat([p, Buffer.from('extra', 'utf8')]), 'EAUTH');
tamper('伪造：随机 64 字节数据包 -> 拒绝',
  (p) => {
    // 保留合法头部、其余全部随机：头部结构合法，因此应由 MAC 判定拒绝
    const q = crypto.randomBytes(64);
    p.subarray(0, H).copy(q, 0);
    return q;
  }, 'EAUTH');

ok('伪造：完全随机 64 字节（头部通常非法）-> EBADHDR 或 EAUTH，总之拒绝', () => {
  let seen = 0;
  for (let i = 0; i < 50; i += 1) {
    try {
      codec.open(crypto.randomBytes(64), refAad);
      throw new assert.AssertionError({ message: '随机包不应解密成功' });
    } catch (err) {
      assert.ok(err instanceof EtmError, '必须是 EtmError');
      assert.ok(err.code === 'EBADHDR' || err.code === 'EAUTH', `code 只能是 EBADHDR/EAUTH，实际 ${err.code}`);
      seen += 1;
    }
  }
  assert.strictEqual(seen, 50);
});

console.log('\n三、为什么“单纯拼接”会让字段可被调换（长度前缀的作用）');

ok('朴素 concat(A,B) 存在跨字段碰撞：不同 (A,B) 拼成同一字节串', () => {
  const naive = (a, b) => Buffer.concat([a, b]);
  const s1 = naive(Buffer.from('ab', 'utf8'), Buffer.from('c', 'utf8'));
  const s2 = naive(Buffer.from('a', 'utf8'), Buffer.from('bc', 'utf8'));
  assert.ok(s1.equals(s2), '朴素拼接：("ab","c") 与 ("a","bc") 无法区分 -> 字段边界可被挪动');
});

ok('带长度前缀的规范编码：字段划分唯一，无碰撞', () => {
  const empty = Buffer.alloc(0);
  // canonicalize 计算的是 (header, nonce, ciphertext, aad) 的认证输入；
  // 这里取两个仅在“密文/AAD 切分”上不同的组合，证明其认证输入不同。
  const m1 = canonicalize(empty, empty, Buffer.from('ab'), Buffer.from('c'));
  const m2 = canonicalize(empty, empty, Buffer.from('a'), Buffer.from('bc'));
  assert.ok(!m1.equals(m2), '长度前缀使 ("ab","c") 与 ("a","bc") 的认证输入不同 -> HMAC 不同 -> 拒绝');
});

ok('不同字段顺序产生不同的 MAC 输入（顺序也被绑定）', () => {
  const x = Buffer.from('XXXXXXXX', 'utf8');
  const y = Buffer.from('YYYYYYYY', 'utf8');
  const empty = Buffer.alloc(0);
  const orderA = canonicalize(empty, empty, x, y);
  const orderB = canonicalize(empty, empty, y, x);
  assert.ok(!orderA.equals(orderB));
});

console.log('\n四、组合正确性的可交叉验证');

ok('多组随机往返（含随机 keyId 与随机 AAD）', () => {
  for (let i = 0; i < 200; i += 1) {
    const c = createCodec(randomKey());
    const pt = crypto.randomBytes(Math.floor(Math.random() * 300));
    const aad = crypto.randomBytes(Math.floor(Math.random() * 64));
    const kid = Math.floor(Math.random() * 0xffffff);
    const pkt = c.seal(pt, aad, kid);
    assert.ok(c.open(pkt, aad).equals(pt));
  }
});

console.log(`\n结果：${passed} 通过，${failures.length} 失败`);
if (failures.length > 0) {
  process.exit(1);
}
