"""Weighted Fair Queuing (WFQ family) scheduler + packet-level simulator.

Algorithm: Self-Clocked Fair Queuing (SCFQ), a virtual-time based member of
the WFQ family. Each packet gets a virtual finish tag

    F = max(V, F_prev_of_same_queue) + L / w

where V is the virtual time (finish tag of the packet currently/last in
service), L the packet length and w the queue weight. The server always
serves the head-of-queue packet with the smallest F.

Key properties:
  * Work-conserving: an empty queue is simply absent from the eligible set,
    so its share is instantly usable by backlogged queues.
  * A queue that becomes idle and later returns gets F based on the *current*
    virtual time (max with V), so it cannot hoard credit while idle and
    monopolize the link on return.
  * Weight 0 is legal and means "best effort": served only when no
    positive-weight queue has a packet ready.

Only the Python 3 standard library is used.
"""

from collections import deque
import heapq


class QueueConfigError(ValueError):
    pass


class _Queue:
    __slots__ = ("name", "weight", "packets", "last_finish")

    def __init__(self, name, weight):
        self.name = name
        self.weight = weight
        self.packets = deque()   # (arrival_time, size, seq)
        self.last_finish = 0.0


class WFQSimulator:
    """Event-driven, non-preemptive packet-by-packet WFQ/SCFQ simulator.

    Time is in arbitrary units; the link serves `rate` bytes per unit time.
    """

    def __init__(self, rate=1.0):
        if rate <= 0:
            raise QueueConfigError("link rate must be positive")
        self.rate = float(rate)
        self._queues = {}
        self._arrivals = []      # (time, seq, qname, size)
        self._seq = 0
        self._ran = False
        # results
        self.completions = {}    # qname -> [(depart_time, size)]
        self.service_starts = {} # qname -> [start_time]
        self.waits = {}          # qname -> [start - arrival]
        self.finish_time = 0.0

    # ------------------------------------------------------------------ API
    def add_queue(self, name, weight):
        if name in self._queues:
            raise QueueConfigError("duplicate queue %r" % name)
        if weight < 0:
            raise QueueConfigError("weight must be >= 0 (0 = best effort)")
        self._queues[name] = _Queue(name, float(weight))
        return self

    def submit(self, queue, size, time=0.0):
        """Schedule a packet of `size` bytes to arrive at `time`."""
        if queue not in self._queues:
            raise QueueConfigError("unknown queue %r" % queue)
        if size <= 0:
            raise QueueConfigError("packet size must be positive")
        if self._ran:
            raise QueueConfigError("cannot submit after run()")
        self._arrivals.append((float(time), self._seq, queue, float(size)))
        self._seq += 1
        return self

    def run(self):
        self._ran = True
        arrivals = sorted(self._arrivals)
        idx = 0
        n = len(arrivals)
        virtual_time = 0.0
        server_free = 0.0
        now = 0.0
        heap = []  # (prio_class, finish_tag, seq, qname); class 0 = weight>0

        def make_eligible(q):
            head = q.packets[0]
            w = q.weight if q.weight > 0 else 1.0
            tag = max(virtual_time, q.last_finish) + head[1] / w
            q.last_finish = tag
            prio = 0 if q.weight > 0 else 1
            heapq.heappush(heap, (prio, tag, head[2], q.name))

        for q in self._queues.values():
            self.completions[q.name] = []
            self.service_starts[q.name] = []
            self.waits[q.name] = []

        while idx < n or heap:
            # Advance decision time: when the server next picks a packet.
            if heap:
                now = max(now, server_free)
            else:
                now = max(now, server_free, arrivals[idx][0])
            # Admit every packet that has arrived by the decision time.
            while idx < n and arrivals[idx][0] <= now:
                atime, aseq, qname, asize = arrivals[idx]
                q = self._queues[qname]
                was_empty = not q.packets
                q.packets.append((atime, asize, aseq))
                # tag assignment happens when the packet becomes head
                if was_empty and len(q.packets) == 1:
                    make_eligible(q)
                idx += 1
            if not heap:
                continue
            _, tag, _, qname = heapq.heappop(heap)
            q = self._queues[qname]
            arr, size, _ = q.packets.popleft()
            start = now
            depart = start + size / self.rate
            server_free = depart
            virtual_time = tag  # self-clocked virtual time
            self.completions[qname].append((depart, size))
            self.service_starts[qname].append(start)
            self.waits[qname].append(start - arr)
            if q.packets:
                make_eligible(q)
            self.finish_time = depart
        return Stats(self)


class Stats:
    """Measured results of a simulation run."""

    def __init__(self, sim):
        self._sim = sim

    def bytes_served(self, qname, t0=0.0, t1=None):
        t1 = float("inf") if t1 is None else t1
        return sum(s for t, s in self._sim.completions[qname] if t0 <= t <= t1)

    def share(self, qname, t0=0.0, t1=None):
        """Fraction of all served bytes (in window) delivered to `qname`."""
        total = sum(self.bytes_served(q, t0, t1) for q in self._sim.completions)
        return self.bytes_served(qname, t0, t1) / total if total else 0.0

    def throughput(self, qname, t0, t1):
        """Bytes per time unit completed inside [t0, t1]."""
        return self.bytes_served(qname, t0, t1) / (t1 - t0)

    def max_wait(self, qname):
        """Max (service start - arrival) over the queue's packets."""
        w = self._sim.waits[qname]
        return max(w) if w else 0.0

    def max_service_gap(self, qname):
        """Max gap between consecutive service starts of the queue.

        This is the 'never starved' metric: even under extreme weight
        ratios and bursts, a backlogged queue is revisited regularly.
        """
        starts = self._sim.service_starts[qname]
        if len(starts) < 2:
            return 0.0
        return max(b - a for a, b in zip(starts, starts[1:]))
