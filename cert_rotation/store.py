"""Certificate store: active cert set, traffic weights, live connections.

Safety invariants enforced here (they are what guarantees there is never a
window where no certificate is serving):

* apply_weights() refuses an empty weight set and refuses weights that do
  not sum to 100 -- so at least one cert always serves new handshakes.
* retire() refuses while the cert still has weight or live connections --
  so established connections keep their negotiated cert until they close.
"""
import random
from typing import Dict, Optional

from .certs import Certificate
from .errors import RotationError


class Connection:
    """An established connection. It pins the certificate it negotiated;
    weight changes only affect *new* handshakes, never this object."""

    __slots__ = ("conn_id", "cert", "opened_at", "closed")

    def __init__(self, conn_id: int, cert: Certificate, opened_at: float):
        self.conn_id = conn_id
        self.cert = cert
        self.opened_at = opened_at
        self.closed = False

    def is_usable(self, store: "CertStore") -> bool:
        return (not self.closed) and (not store.is_retired(self.cert.cert_id))


class CertStore:
    def __init__(self, clock, rng: Optional[random.Random] = None):
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._certs: Dict[str, Certificate] = {}
        self._retired = set()
        self._weights: Dict[str, int] = {}  # only positive weights stored
        self._connections: Dict[int, Connection] = {}
        self._next_conn_id = 0

    # ---- certificates -------------------------------------------------
    def add_cert(self, cert: Certificate) -> None:
        if cert.cert_id in self._retired:
            raise RotationError(f"cert {cert.cert_id} already retired")
        self._certs[cert.cert_id] = cert

    def bootstrap(self, cert: Certificate) -> None:
        if self._weights:
            raise RotationError("store already bootstrapped")
        self.add_cert(cert)
        self.apply_weights({cert.cert_id: 100})

    def is_retired(self, cert_id: str) -> bool:
        return cert_id in self._retired

    # ---- weights ------------------------------------------------------
    def apply_weights(self, weights: Dict[str, int]) -> None:
        cleaned: Dict[str, int] = {}
        for cid, w in weights.items():
            w = int(w)
            if w < 0:
                raise RotationError(f"negative weight for {cid}")
            if w == 0:
                continue
            if cid not in self._certs:
                raise RotationError(f"unknown cert {cid}")
            if cid in self._retired:
                raise RotationError(f"cert {cid} is retired")
            cleaned[cid] = w
        if not cleaned:
            raise RotationError(
                "refusing empty weight set: would open a no-certificate window")
        if sum(cleaned.values()) != 100:
            raise RotationError("weights must sum to 100")
        self._weights = cleaned  # atomic swap of the whole set

    def weights(self) -> Dict[str, int]:
        return dict(self._weights)

    def has_gap(self) -> bool:
        """True if NO certificate would serve a new handshake right now."""
        return not any(w > 0 for w in self._weights.values())

    # ---- connections --------------------------------------------------
    def open_connection(self) -> Connection:
        if self.has_gap():
            raise RotationError("no active certificate: cannot open connection")
        r = self._rng.uniform(0, 100)
        acc = 0.0
        chosen = None
        for cid, w in self._weights.items():
            acc += w
            if r < acc:
                chosen = cid
                break
        if chosen is None:  # float rounding guard
            chosen = next(reversed(self._weights))
        self._next_conn_id += 1
        conn = Connection(self._next_conn_id, self._certs[chosen], self._clock.now())
        self._connections[conn.conn_id] = conn
        return conn

    def close_connection(self, conn: Connection) -> None:
        conn.closed = True
        self._connections.pop(conn.conn_id, None)

    def connections_using(self, cert_id: str) -> int:
        return sum(1 for c in self._connections.values()
                   if not c.closed and c.cert.cert_id == cert_id)

    # ---- retirement ---------------------------------------------------
    def retire(self, cert_id: str) -> bool:
        """Retire only when the cert has no weight and no live connections."""
        if cert_id in self._retired:
            return True
        if self._weights.get(cert_id, 0) > 0:
            return False
        if self.connections_using(cert_id) > 0:
            return False
        self._retired.add(cert_id)
        return True
