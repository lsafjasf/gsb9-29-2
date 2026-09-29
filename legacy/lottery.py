"""Legacy lucky-draw helper (module A).

Draws from the global random module, so results depend on whatever the
process last seeded. Silently returns the whole population when asked for
more winners than records.
"""

import random


def pick_winners(records, k):
    """Pick k distinct winners uniformly at random."""
    pool = list(records)
    random.shuffle(pool)
    return pool[:k]
