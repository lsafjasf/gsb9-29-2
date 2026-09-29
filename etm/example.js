'use strict';

// 最小可用示例：node example.js
const { createCodec, randomKey } = require('./etm');

const masterKey = randomKey();              // 32 字节，妥善保管（KMS/密钥文件）
const codec = createCodec(masterKey);

const plaintext = Buffer.from('转账 100 元给 alice', 'utf8');
const aad = Buffer.from('session=42; sender=bob', 'utf8'); // 关联数据，不加密但必须一致

const packet = codec.seal(plaintext, aad, /* keyId */ 7);
console.log('封装包(hex) =', packet.toString('hex'));

const recovered = codec.open(packet, aad);
console.log('解密结果    =', recovered.toString('utf8'));

try {
  codec.open(packet, Buffer.from('tampered-aad'));
} catch (err) {
  console.log(`篡改被拒绝  : [${err.code}] ${err.message}`);
}
