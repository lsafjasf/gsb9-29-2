#!/usr/bin/env python3
"""Run the full parser test-suite, then print the transition table and
transition-coverage report."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(ROOT, "tests"))
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)

    from fsm_parser import transition_table_markdown
    print("\n== Transition table (state x event) ==\n")
    print(transition_table_markdown())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
