"""Frame format, authenticated encryption and deduplication window.

The same datagram is sent identically over every active link; the remote end
accepts the first copy of each sequence number and drops the rest. That is
why the effective latency is the minimum across the links.

Frame:

    offset  bytes  field
    0       1      version
    1       1      kind (DATA / PING / PONG)
    2       2      flags (FLAG_DUP: the sender sent it over several links)
    4       2      client_id: which VPS player (in both directions)
    6       8      sender's epoch (uint32 unix seconds + 4 random bytes)
    14      8      seq (uint64, big endian)
    22      ...    ChaCha20-Poly1305(ciphertext || 16-byte tag)

The header travels in clear but is authenticated as AAD, so it cannot be
altered without invalidating the tag. The nonce is derived from (direction,
seq) and each frame is sealed only once: the copies over different links are
byte-for-byte identical.

Epochs. ``seq`` starts at 0 every time an end starts, so with a fixed key a
restart would repeat nonces, which in ChaCha20-Poly1305 breaks both
confidentiality and authentication. That is why every sender picks a new epoch
on startup and encrypts with a key derived from (secret, epoch, direction). The
epoch travels in the header; the receiver derives the matching key and, if the
frame authenticates, adopts the new epoch with a clean anti-replay window. So a
restart of either side recovers on its own, without a handshake.

To keep a captured old epoch from being replayed, the receiver accepts neither
an epoch older than the current one nor an already retired one, and one
starting from scratch (no current epoch) requires the timestamp to be within
``max_skew_s`` of its clock. That last check is used by the server; the client
turns it off (``max_skew_s=None``) because the server's epoch dates from when
the service started, which may be days ago.

Several players. Each player has their own secret, and their ``client_id``
travels in the header: the server finds the session by that number before
decrypting, without trying keys. The id also goes into the key derivation, so
two players share no key material even if they were configured with the same
secret. Within one player, two client processes with different epochs would
trample each other's window; that is why the client uses a single
:class:`Codec` for data and probes.
"""

from __future__ import annotations

import os
import struct
import time
from collections import deque
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 3

KIND_DATA = 0
KIND_PING = 1
KIND_PONG = 2

#: the frame was sent over more than one link. Tells the server which paths
#: carry only duplicated traffic and which one is the primary.
FLAG_DUP = 0x0001

#: client -> server
DIR_UP = 0
#: server -> client
DIR_DOWN = 1

HEADER = struct.Struct("!BBHH8sQ")
HEADER_LEN = HEADER.size  # 22
MAX_CLIENT_ID = 0xFFFF
EPOCH_LEN = 8
TAG_LEN = 16
#: header + AEAD tag; add 28 (UDP+IP) for the cost against the outer MTU
OVERHEAD = HEADER_LEN + TAG_LEN

#: size of the anti-replay / deduplication window, in packets
DEFAULT_WINDOW = 8192

#: clock tolerance for accepting the epoch of a peer with no current epoch
DEFAULT_MAX_SKEW_S = 600.0
#: retired epochs remembered so their replay can be rejected
_RETIRED_EPOCHS = 32

_HKDF_INFO = b"antilag v1 datapath key"
_HKDF_EPOCH_INFO = b"antilag v2 epoch key"


class ProtocolError(Exception):
    """Unreadable frame: foreign version, invalid length or a tag that does not match."""


def derive_key(secret: str | bytes, salt: bytes = b"") -> bytes:
    """Derive the 32-byte datapath key from the shared phrase."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not secret:
        raise ValueError("el secreto compartido no puede estar vacio")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO,
    ).derive(secret)


def generate_secret() -> str:
    """Generate a production-grade shared phrase (32 bytes in hex)."""
    return os.urandom(32).hex()


def new_epoch(now: float | None = None) -> bytes:
    """8-byte epoch: unix seconds (uint32) followed by 4 random bytes."""
    seconds = int(time.time() if now is None else now) & 0xFFFFFFFF
    return struct.pack("!I", seconds) + os.urandom(4)


def epoch_time(epoch: bytes) -> int:
    return struct.unpack("!I", epoch[:4])[0]


def peek_client_id(datagram: bytes) -> int:
    """A frame's ``client_id``, without decrypting it.

    Only used to choose the session: the id is authenticated as part of the
    header, so if someone changes it the frame does not open with any key.
    """
    if len(datagram) < HEADER_LEN + TAG_LEN:
        raise ProtocolError("datagrama mas corto que cabecera + tag")
    if datagram[0] != VERSION:
        raise ProtocolError(_version_message(datagram[0]))
    return struct.unpack_from("!H", datagram, 4)[0]


def _version_message(version: int) -> str:
    return (f"version de protocolo {version}, este extremo usa {VERSION}: "
            "actualiza antilag en el VPS y en el PC")


def _epoch_key(master: bytes, epoch: bytes, direction: int,
               client_id: int = 0) -> ChaCha20Poly1305:
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=epoch,
        info=_HKDF_EPOCH_INFO + bytes([direction]) + struct.pack("!H", client_id),
    ).derive(master)
    return ChaCha20Poly1305(key)


def _nonce(direction: int, seq: int) -> bytes:
    # 12 bytes: direction (1) + padding (3) + seq (8). Unique per (direction,
    # seq), and since each seq carries a single inner packet it is never reused
    # with a different plaintext.
    return struct.pack("!BxxxQ", direction, seq)


class ReplayWindow:
    """IPsec-style sliding window: deduplicates and rejects replays.

    ``bitmap`` keeps one bit per sequence already seen; bit 0 corresponds to
    ``highest``. Accepts reordering within the window and drops anything too
    old.
    """

    __slots__ = ("window", "highest", "bitmap", "_mask")

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        if window < 1:
            raise ValueError("la ventana debe ser positiva")
        self.window = window
        self.highest = -1
        self.bitmap = 0
        self._mask = (1 << window) - 1

    def check_and_set(self, seq: int) -> bool:
        """Return True if ``seq`` is seen for the first time (it must be processed)."""
        if seq < 0 or seq > 0xFFFFFFFFFFFFFFFF:
            return False

        if self.highest < 0:
            self.highest = seq
            self.bitmap = 1
            return True

        if seq > self.highest:
            shift = seq - self.highest
            if shift >= self.window:
                self.bitmap = 1
            else:
                self.bitmap = ((self.bitmap << shift) | 1) & self._mask
            self.highest = seq
            return True

        offset = self.highest - seq
        if offset >= self.window:
            return False  # left of the window
        probe = 1 << offset
        if self.bitmap & probe:
            return False  # duplicate copy
        self.bitmap |= probe
        return True


@dataclass(frozen=True)
class Frame:
    kind: int
    seq: int
    payload: bytes
    flags: int = 0


class Codec:
    """Seals and opens frames for one end of the tunnel.

    ``direction`` is the one this end uses when sending; the opposite is
    expected when receiving, which keeps a reflected own frame from being
    accepted as the peer's.
    """

    def __init__(
        self,
        key: bytes,
        direction: int,
        window: int = DEFAULT_WINDOW,
        *,
        client_id: int = 0,
        epoch: bytes | None = None,
        max_skew_s: float | None = DEFAULT_MAX_SKEW_S,
        clock=time.time,
    ) -> None:
        if len(key) != 32:
            raise ValueError("la clave debe ser de 32 bytes; usa derive_key()")
        if direction not in (DIR_UP, DIR_DOWN):
            raise ValueError("direccion invalida")
        if not 0 <= client_id <= MAX_CLIENT_ID:
            raise ValueError("client_id fuera de rango")
        if epoch is not None and len(epoch) != EPOCH_LEN:
            raise ValueError(f"el epoch debe ser de {EPOCH_LEN} bytes")
        self._master = key
        self.client_id = client_id
        self.send_dir = direction
        self.recv_dir = DIR_DOWN if direction == DIR_UP else DIR_UP
        self.epoch = epoch if epoch is not None else new_epoch(clock())
        self._send_aead = _epoch_key(key, self.epoch, self.send_dir, client_id)
        self._window_size = window
        self.max_skew_s = max_skew_s
        self._clock = clock
        self._seq = 0
        #: the peer's current epoch, its key and its window
        self.peer_epoch: bytes | None = None
        self._recv_aead: ChaCha20Poly1305 | None = None
        self.window = ReplayWindow(window)
        self._retired: deque[bytes] = deque(maxlen=_RETIRED_EPOCHS)
        # counters that feed the panel
        self.sealed = 0
        self.accepted = 0
        self.duplicates = 0
        self.invalid = 0
        self.stale = 0
        self.peer_restarts = 0

    @property
    def next_seq(self) -> int:
        return self._seq

    def seal(
        self, payload: bytes, kind: int = KIND_DATA, flags: int = 0
    ) -> tuple[int, bytes]:
        """Encrypt ``payload`` once; the datagram is replicated as is on every link."""
        seq = self._seq
        self._seq += 1
        header = HEADER.pack(VERSION, kind, flags, self.client_id, self.epoch, seq)
        body = self._send_aead.encrypt(_nonce(self.send_dir, seq), payload, header)
        self.sealed += 1
        return seq, header + body

    def _epoch_admissible(self, epoch: bytes) -> bool:
        """Whether a frame from a new epoch is worth trying to authenticate."""
        if epoch in self._retired:
            return False
        stamp = epoch_time(epoch)
        if self.max_skew_s is not None:
            now = self._clock()
            if stamp > now + self.max_skew_s:
                return False  # from the future: broken clock or forged frame
            if self.peer_epoch is None:
                return now - stamp <= self.max_skew_s
        if self.peer_epoch is None:
            return True
        return stamp >= epoch_time(self.peer_epoch)

    def open(self, datagram: bytes) -> Frame | None:
        """Decrypt and deduplicate.

        Returns the frame the first time it arrives, or ``None`` if it is a
        duplicate copy or falls outside the window. A corrupt, forged or
        expired-epoch frame raises :class:`ProtocolError`.
        """
        if len(datagram) < HEADER_LEN + TAG_LEN:
            self.invalid += 1
            raise ProtocolError("datagrama mas corto que cabecera + tag")

        header = datagram[:HEADER_LEN]
        version, kind, flags, client_id, epoch, seq = HEADER.unpack(header)
        if version != VERSION:
            self.invalid += 1
            raise ProtocolError(_version_message(version))
        if client_id != self.client_id:
            self.invalid += 1
            raise ProtocolError(f"trama del cliente {client_id}, se esperaba {self.client_id}")

        if epoch == self.peer_epoch:
            aead = self._recv_aead
        elif self._epoch_admissible(epoch):
            aead = _epoch_key(self._master, epoch, self.recv_dir, self.client_id)
        else:
            self.stale += 1
            raise ProtocolError("epoch caducado, retirado o fuera de reloj")

        try:
            payload = aead.decrypt(_nonce(self.recv_dir, seq), datagram[HEADER_LEN:], header)
        except InvalidTag as exc:
            self.invalid += 1
            raise ProtocolError("fallo la autenticacion de la trama") from exc

        if epoch != self.peer_epoch:
            # The peer started again (or this is the first contact): only now,
            # with the frame already authenticated, switch epoch and window.
            if self.peer_epoch is not None:
                self._retired.append(self.peer_epoch)
                self.peer_restarts += 1
            self.peer_epoch = epoch
            self._recv_aead = aead
            self.window = ReplayWindow(self._window_size)

        # Deduplicate only after authenticating: otherwise an attacker could
        # burn sequence numbers and cause legitimate ones to be dropped.
        if not self.window.check_and_set(seq):
            self.duplicates += 1
            return None

        self.accepted += 1
        return Frame(kind=kind, seq=seq, payload=payload, flags=flags)

    def stats(self) -> dict[str, int]:
        return {
            "sealed": self.sealed,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "invalid": self.invalid,
            "stale": self.stale,
            "peer_restarts": self.peer_restarts,
        }
