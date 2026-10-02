#!/usr/bin/env bash
# 一键验证：差分对拍 -> 边界用例 -> 内存峰值。仅依赖 Python 3 标准库。
set -e
cd "$(dirname "$0")"
echo "== 1/3 差分对拍（legacy 大函数 vs 管线，4 组随机种子）=="
for s in 20261002 1 42 999; do python3 tests/difftest.py "$s" | tail -1; done
echo
echo "== 2/3 边界用例（单步/跳过/超大图/失败回滚/缓冲上界）=="
python3 -m unittest discover -s tests -p 'test_*.py'
echo
echo "== 3/3 内存峰值（4000x3000 大图）=="
python3 tests/memtest.py
