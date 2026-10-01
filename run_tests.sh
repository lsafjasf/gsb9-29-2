#!/bin/sh
# 运行全部回归 + 差分测试（仅标准库，Python 3.8+）
cd "$(dirname "$0")"
exec python3 -m unittest discover -s tests -t . -v
