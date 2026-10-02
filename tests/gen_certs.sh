#!/usr/bin/env bash
# 生成对拍用测试证书（仅依赖 openssl CLI）。
# 用法: bash tests/gen_certs.sh [输出目录，默认 testdata/]
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-testdata}"
mkdir -p "$OUT"
cd "$OUT"

OPENSSL=${OPENSSL:-openssl}

gen_key() { # gen_key <name> <rsa|ec>
  if [ "$2" = rsa ]; then
    $OPENSSL genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$1.key" 2>/dev/null
  else
    $OPENSSL ecparam -name prime256v1 -genkey -noout -out "$1.key"
  fi
}

mk_csr() { $OPENSSL req -new -key "$1.key" -out "$1.csr" -subj "$2"; }

to_der() { $OPENSSL x509 -in "$1.pem" -outform DER -out "$1.der"; }

# ---------- 密钥 ----------
gen_key root ec
gen_key sub rsa
gen_key server rsa
gen_key client rsa
gen_key expired rsa
gen_key future rsa
gen_key wrongeku rsa
gen_key rogue rsa
gen_key root2 ec
gen_key sub2 rsa
gen_key leaf2 rsa

# ---------- 根 CA（ECDSA P-256，自签） ----------
mk_csr root "/C=CN/O=Example Root CA/CN=Example Root CA"
$OPENSSL x509 -req -in root.csr -signkey root.key -days 3650 \
  -extfile <(printf 'basicConstraints=critical,CA:TRUE,pathlen:1\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always') \
  -out root.pem 2>/dev/null
to_der root

# ---------- 中间 CA（RSA，根签发，pathlen:0） ----------
mk_csr sub "/C=CN/O=Example/CN=Example Sub CA"
$OPENSSL x509 -req -in sub.csr -CA root.pem -CAkey root.key -CAcreateserial -days 1825 \
  -extfile <(printf 'basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid') \
  -out sub.pem 2>/dev/null
to_der sub

leaf_ext() { # 通用叶子扩展参数
  printf 'basicConstraints=critical,CA:FALSE\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n%b' "$1"
}

# ---------- 服务器叶子（RSA, serverAuth + SAN） ----------
mk_csr server "/C=CN/O=Example/CN=www.example.com"
$OPENSSL x509 -req -in server.csr -CA sub.pem -CAkey sub.key -CAcreateserial -days 825 \
  -extfile <(leaf_ext 'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:www.example.com,DNS:example.com') \
  -out server.pem 2>/dev/null
to_der server

# ---------- 客户端叶子（clientAuth） ----------
mk_csr client "/C=CN/O=Example/CN=Example Client"
$OPENSSL x509 -req -in client.csr -CA sub.pem -CAkey sub.key -CAcreateserial -days 825 \
  -extfile <(leaf_ext 'keyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth') \
  -out client.pem 2>/dev/null
to_der client

# ---------- 过期 / 未生效 / 用途错误（时间字段由 fix_validity.py 改写） ----------
mk_csr expired "/C=CN/O=Example/CN=expired.example.com"
$OPENSSL x509 -req -in expired.csr -CA sub.pem -CAkey sub.key -CAcreateserial -days 30 \
  -extfile <(leaf_ext 'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth') \
  -out expired.pem 2>/dev/null

mk_csr future "/C=CN/O=Example/CN=future.example.com"
$OPENSSL x509 -req -in future.csr -CA sub.pem -CAkey sub.key -CAcreateserial -days 30 \
  -extfile <(leaf_ext 'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth') \
  -out future.pem 2>/dev/null

mk_csr wrongeku "/C=CN/O=Example/CN=wrongeku.example.com"
$OPENSSL x509 -req -in wrongeku.csr -CA sub.pem -CAkey sub.key -CAcreateserial -days 365 \
  -extfile <(leaf_ext 'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth') \
  -out wrongeku.pem 2>/dev/null
to_der wrongeku

# ---------- 无链自签叶子 ----------
mk_csr rogue "/C=CN/O=Example/CN=rogue.example.com"
$OPENSSL x509 -req -in rogue.csr -signkey rogue.key -days 365 \
  -extfile <(leaf_ext 'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth') \
  -out rogue.pem 2>/dev/null
to_der rogue

# ---------- pathlen 违规链：root2(pathlen:0) -> sub2(CA) -> leaf2 ----------
mk_csr root2 "/C=CN/O=Example Root2/CN=Example Root2 CA"
$OPENSSL x509 -req -in root2.csr -signkey root2.key -days 3650 \
  -extfile <(printf 'basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always') \
  -out root2.pem 2>/dev/null
to_der root2

mk_csr sub2 "/C=CN/O=Example/CN=Example Sub2 CA"
$OPENSSL x509 -req -in sub2.csr -CA root2.pem -CAkey root2.key -CAcreateserial -days 1825 \
  -extfile <(printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid') \
  -out sub2.pem 2>/dev/null
to_der sub2

mk_csr leaf2 "/C=CN/O=Example/CN=leaf2.example.com"
$OPENSSL x509 -req -in leaf2.csr -CA sub2.pem -CAkey sub2.key -CAcreateserial -days 365 \
  -extfile <(leaf_ext 'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth') \
  -out leaf2.pem 2>/dev/null
to_der leaf2

# ---------- 时间改写（等长替换 UTCTime，保持 DER 与签名有效） ----------
python3 ../tests/fix_validity.py expired.pem 200101000000Z 200201000000Z
python3 ../tests/fix_validity.py future.pem  320101000000Z 330101000000Z
to_der expired
to_der future

# ---------- 清理中间文件 ----------
rm -f *.csr *.srl
echo "生成完成: $(ls *.der | wc -l) 个 DER 证书于 $OUT/"
