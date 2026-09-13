"""WINK v0.2 — shared link-layer primitives.

Packet framing, CRC-16(-ish, via CRC32-truncated as v0.1 did), and the
K=7 rate-1/2 convolutional code + Viterbi decoder. These are unchanged
from v0.1 (wink_v01/wink_modem.py) and are physical-layer independent,
so they're factored out here and reused by every waveform candidate in
the v0.2 bake-off / modem.
"""

from __future__ import annotations
import math, struct, zlib
from dataclasses import dataclass

MAGIC = b"WN"
VERSION = 1
MAX_PAYLOAD = 32


def callsign_id(callsign: str) -> int:
    return zlib.crc32(callsign.upper().encode("ascii")) & 0xFFFFFF


def crc16(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFF


def bytes_to_bits(data: bytes) -> list[int]:
    return [(b >> (7 - i)) & 1 for b in data for i in range(8)]


def bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits), 8):
        chunk = bits[i:i + 8]
        if len(chunk) < 8:
            chunk += [0] * (8 - len(chunk))
        v = 0
        for bit in chunk:
            v = (v << 1) | bit
        out.append(v)
    return bytes(out)


DEFAULT_TTL = 4  # max flood-relay hops before a packet is dropped


def crc_packet_body(kind, payload, source, dest, seq, ttl=DEFAULT_TTL):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    return struct.pack(">2sBBBBIII", MAGIC, VERSION, kind, len(payload), ttl,
                        source, dest, seq) + payload


@dataclass
class Packet:
    kind: int
    source: int
    dest: int
    seq: int
    payload: bytes
    ttl: int = DEFAULT_TTL  # was an unused reserved byte; now hop budget for
                            # opt-in flood relaying (see wink_mesh.py). Not a
                            # breaking wire-format change -- same byte, same
                            # position, previously always sent as 0.

    def encode(self) -> bytes:
        body = crc_packet_body(self.kind, self.payload,
                                self.source, self.dest, self.seq, self.ttl)
        return body + struct.pack(">H", crc16(body))

    @staticmethod
    def decode(raw: bytes) -> "Packet":
        if len(raw) < 20:
            raise ValueError("packet too short")
        magic, version, kind, length, ttl, source, dest, seq = struct.unpack(
            ">2sBBBBIII", raw[:18])
        if magic != MAGIC or version != VERSION:
            raise ValueError("bad header")
        end = 18 + length
        if len(raw) < end + 2:
            raise ValueError("truncated packet")
        body = raw[:end]
        wanted = struct.unpack(">H", raw[end:end + 2])[0]
        if crc16(body) != wanted:
            raise ValueError("CRC failure")
        return Packet(kind, source, dest, seq, raw[18:end], ttl=ttl)


# ---------- convolutional code (parameterised; rate-generic) ----------
#
# K7/K9 are rate 1/2 (dfree 10/12). K9_1_3 is rate 1/3, Voyager-standard
# generators 557/663/711 octal, for ~1.5-2 dB more coding gain at 1.5x
# time-on-air vs rate 1/2. Use when you want really high success rate.

FEC_CONFIGS = {
    "K7":     dict(K=7, Gs=(0o171, 0o133),               G0=0o171, G1=0o133),  # dfree 10
    "K9":     dict(K=9, Gs=(0o753, 0o561),               G0=0o753, G1=0o561),  # dfree 12
    "K9_1_3": dict(K=9, Gs=(0o557, 0o663, 0o711),         G0=0o557, G1=0o663, G2=0o711),  # rate 1/3
}
# legacy aliases for old defaults
K = 7
G0, G1 = 0o171, 0o133


def parity(x: int) -> int:
    return x.bit_count() & 1


def _resolve_fec(K=None, Gs=None, G0=None, G1=None, G2=None):
    if Gs is not None:
        return K if K is not None else 9, tuple(Gs)
    if G0 is not None:
        gs = tuple(g for g in (G0, G1, G2) if g is not None)
        return K if K is not None else (7 if len(gs)==2 and gs[0]==0o171 else 9), gs
    # default: K9 rate 1/2 (project default per wink_profiles.DEFAULT_FEC)
    return 9, FEC_CONFIGS["K9"]["Gs"]


def conv_encode(bits: list[int], terminate=True, K: int = None, Gs=None, G0: int = None, G1: int = None, G2: int = None) -> list[int]:
    K, Gs = _resolve_fec(K, Gs, G0, G1, G2)
    state = 0
    seq = list(bits)
    if terminate:
        seq += [0] * (K - 1)
    out = []
    for b in seq:
        reg = (state << 1) | b
        out.extend(parity(reg & G) for G in Gs)
        state = reg & ((1 << (K - 1)) - 1)
    return out


def viterbi_decode_soft(llrs: list[float], n_input_bits: int,
                         K: int = None, Gs=None, G0: int = None, G1: int = None, G2: int = None) -> list[int]:
    K, Gs = _resolve_fec(K, Gs, G0, G1, G2)
    rate = len(Gs)
    nsteps = len(llrs) // rate
    INF = 1e30
    metrics = [INF] * (1 << (K - 1))
    metrics[0] = 0.0
    prev_state = [[0] * (1 << (K - 1)) for _ in range(nsteps)]
    prev_bit = [[0] * (1 << (K - 1)) for _ in range(nsteps)]

    for t in range(nsteps):
        nxt = [INF] * (1 << (K - 1))
        obs = llrs[t*rate:(t+1)*rate]
        for state, pm in enumerate(metrics):
            if pm >= INF:
                continue
            for bit in (0, 1):
                reg = (state << 1) | bit
                cost = pm
                for i, G in enumerate(Gs):
                    out = parity(reg & G)
                    cost += (-obs[i] if out else obs[i])
                ns = reg & ((1 << (K - 1)) - 1)
                if cost < nxt[ns]:
                    nxt[ns] = cost
                    prev_state[t][ns] = state
                    prev_bit[t][ns] = bit
        metrics = nxt

    state = 0
    decoded = []
    for t in range(nsteps - 1, -1, -1):
        decoded.append(prev_bit[t][state])
        state = prev_state[t][state]
    decoded.reverse()
    return decoded[:n_input_bits]


# ---------- interleaver ----------

def interleave(bits: list[int], rows: int = 8) -> list[int]:
    cols = math.ceil(len(bits) / rows)
    padded = bits + [0] * (rows * cols - len(bits))
    return [padded[r * cols + c] for c in range(cols) for r in range(rows)]


def deinterleave(bits: list[int], rows: int = 8, original_len=None) -> list[int]:
    cols = math.ceil(len(bits) / rows)
    matrix = [[0] * cols for _ in range(rows)]
    k = 0
    for c in range(cols):
        for r in range(rows):
            matrix[r][c] = bits[k]
            k += 1
    out = [matrix[r][c] for r in range(rows) for c in range(cols)]
    return out if original_len is None else out[:original_len]


# ---------- generic Gray coding (any bit width, not just 3-bit) ----------

def gray_encode(x: int) -> int:
    return x ^ (x >> 1)


def gray_decode(g: int) -> int:
    x = g
    mask = g
    while mask:
        mask >>= 1
        x ^= mask
    return x
