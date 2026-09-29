"""演示：消息认证 + 从主密钥派生多把用途隔离的子密钥。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mac_kdf import derive_key, hmac_sha256, hmac_verify

master = os.urandom(32)  # 主密钥，现实中应来自 KMS/安全存储

# 1) 派生不同用途的子密钥：标签/上下文不同 -> 密钥必然不同
with derive_key(master, label=b"encryption", context=b"user:alice") as k_enc, \
     derive_key(master, label=b"mac",        context=b"user:alice") as k_mac, \
     derive_key(master, label=b"mac",        context=b"user:bob")   as k_bob:
    print("enc(alice) :", k_enc.bytes().hex()[:16], "...")
    print("mac(alice) :", k_mac.bytes().hex()[:16], "...")
    print("mac(bob)   :", k_bob.bytes().hex()[:16], "...")
    assert k_enc.bytes() != k_mac.bytes() != k_bob.bytes()

    # 2) 用 mac 子密钥做消息认证（HMAC 结构天然抗长度扩展）
    message = b"transfer 100 CNY to bob"
    tag = hmac_sha256(k_mac, message)
    print("tag        :", tag.hex()[:32], "...")
    assert hmac_verify(k_mac, message, tag)
    assert not hmac_verify(k_mac, message + b"!", tag)  # 篡改即失败
    print("verify ok; tampered message rejected")

# 3) with 块退出时子密钥已自动清零
assert k_enc.wiped and k_mac.wiped and k_bob.wiped
print("subkeys wiped on exit")
