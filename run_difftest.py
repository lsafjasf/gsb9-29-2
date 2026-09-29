#!/usr/bin/env python3
"""差分测试入口：python3 run_difftest.py --cases 2000 --seed 1"""

import sys

from difftest.cli import main

if __name__ == "__main__":
    sys.exit(main())
