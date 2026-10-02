#!/usr/bin/env bash
# Run the full suite: unit/edge cases, differential tests, memory benchmark.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v "$@"
echo
echo "=== memory benchmark (streaming vs naive) ==="
python3 tests/test_memory.py
