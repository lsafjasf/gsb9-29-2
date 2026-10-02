"""Injectable clock and network model for the synchronizer.

The server clock is the reference (true time).  The local clock has a
constant initial offset, a constant skew (drift) and may receive discrete
steps.  The network has independent forward/backward delay distributions
(asymmetric RTT), gaussian/uniform jitter, occasional large spikes and
scheduled outages.
"""

from __future__ import annotations

import random

from timesync import measure


class LocalClock:
    def __init__(self, offset=0.0, skew=0.0):
        self.offset0 = offset   # local - server at t=0 (s)
        self.skew = skew        # s/s
        self.steps = []         # [(true_time, delta_to_local_clock)]

    def add_step(self, t_true, delta):
        self.steps.append((t_true, delta))
        self.steps.sort()

    def theta(self, t_true):
        """Instantaneous local-minus-server clock offset (s)."""
        total = self.offset0 + self.skew * t_true
        for t_step, delta in self.steps:
            if t_true >= t_step:
                total += delta
        return total

    def read(self, t_true):
        return t_true + self.theta(t_true)


class Network:
    def __init__(
        self,
        seed=0,
        fwd=0.02,
        bwd=0.02,
        jitter=1e-3,
        spike_prob=0.0,
        spike_mag=0.1,
        outages=(),
    ):
        self.rng = random.Random(seed)
        self.fwd = fwd
        self.bwd = bwd
        self.jitter = jitter
        self.spike_prob = spike_prob
        self.spike_mag = spike_mag
        self.outages = list(outages)

    def _down(self, t):
        return any(start <= t < end for start, end in self.outages)

    def delays(self, t):
        """Return (forward_delay, backward_delay); raise on outage."""
        if self._down(t):
            raise TimeoutError("link unavailable")

        def one_way(base):
            d = base + self.rng.uniform(-self.jitter, self.jitter)
            if self.rng.random() < self.spike_prob:
                d += self.spike_mag
            return max(d, 0.0)

        return one_way(self.fwd), one_way(self.bwd)


class Scenario:
    def __init__(self, clock, network):
        self.clock = clock
        self.network = network

    def exchange(self, t_true):
        """Perform one exchange initiated at true time ``t_true``.

        Returns the NTP 4-tuple
        (local_send, server_recv, server_send, local_recv).
        """
        df, db = self.network.delays(t_true)
        local_send = self.clock.read(t_true)
        server_recv = t_true + df
        server_send = server_recv  # zero server processing time
        local_recv = self.clock.read(t_true + df + db)
        return local_send, server_recv, server_send, local_recv

    def true_offset(self, t_true):
        """Value that should be added to the local clock to read server time."""
        return -self.clock.theta(t_true)


def run(scenario, sync, t_end, dt=1.0, t_start=0.0, on_row=None):
    """Drive ``sync`` with exchanges from ``scenario`` and return last t."""
    t = t_start
    while t <= t_end:
        try:
            l0, s1, s2, l3 = scenario.exchange(t)
            offset, delay = measure(l0, s1, s2, l3)
            sync.update(l3, offset, delay)
        except TimeoutError:
            sync.note_poll_failure(scenario.clock.read(t))
        if on_row is not None:
            on_row(t, sync, scenario)
        t += dt
    return t
