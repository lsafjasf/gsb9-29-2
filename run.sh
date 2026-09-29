#!/usr/bin/env bash
# 一键运行：自测 + 基准/画像
set -euo pipefail
cd "$(dirname "$0")"
echo "== selftest =="
node src/selftest.js
echo
echo "== bench =="
node src/bench.js "$@"
