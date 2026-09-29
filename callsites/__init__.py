"""Refactored call sites: thin adapters over the unified sampling component.

Each function keeps its legacy name and positional arguments; the random
source is now injected explicitly via the keyword-only ``rng`` parameter.
"""
