"""Buggy barrier implementation, kept only to reproduce the production issue.

Defects intentionally preserved:
1. No generation concept: the "round is over" signal is count==0, but the
   releaser resets count and a fast thread can re-arrive (count=1) BEFORE
   slow waiters observe the 0. Those waiters then sleep forever waiting
   for a 0 that already came and went -> lost wakeup / deadlock.
2. No exception/timeout cleanup: if a participant arrives and then dies,
   count leaks into the next round and the barrier trips early with fewer
   than `parties` real participants.
"""

import threading


class BuggyBarrier:
    def __init__(self, parties):
        if parties < 1:
            raise ValueError("parties must be >= 1")
        self.parties = parties
        self.count = 0
        self.cond = threading.Condition()

    def wait(self):
        with self.cond:
            self.count += 1
            if self.count >= self.parties:
                # defect 1: reset is the only signal; no generation tagging
                self.count = 0
                self.cond.notify_all()
            else:
                # wait until the round resets count to 0; a fast thread may
                # already have started the next round (count>0 again) before
                # we observe the 0 -> we miss the wakeup
                while self.count != 0:
                    self.cond.wait()
                # defect 2: no cleanup if we leave via exception
