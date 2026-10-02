#!/usr/bin/env bash
# 全量自测（仅需 Python 3，无第三方依赖）
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v
