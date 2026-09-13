"""WINK-BEACON — a compact, WSPR-competitive beacon mode.

The general WINK Packet format (wink_core.Packet) has ~20 bytes of
framing overhead (magic, version, kind, length, source/dest IDs,
sequence, CRC) -- reasonable for a chat packet, hopeless for a beacon
that needs to squeeze into the same time/bandwidth budget as WSPR.
This module defines a dedicated, compact beacon message (callsign +
4-char locator + power level, ~64 raw bits) and reuses the v0.2
physical layer at WSPR's own symbol rate (1.4648 baud = 12000/8192,
same 8192-sample symbol WSPR uses) so the comparison is apples-to-apples.
"""
from __future__ import annotations
import struct, zlib, math
import numpy as np

from wink_core import conv_encode, viterbi_decode_soft, interleave, deinterleave, bytes_to_bits, bits_to_bytes, FEC_CONFIGS
from wink_modem2 import (
    Candidate, generate_waveform, add_awgn, tone_energies, find_sync,
    soft_llrs_from_energies, make_sync, bits_to_symbols_gray, symbols_to_gray_bits,
)

# Same symbol rate/spacing as WSPR: 4-FSK, spacing == baud (orthogonal),
# 8192-sample symbols at 12000 Hz.
WINK_BEACON = Candidate("WINK-BEACON", M=4, baud=12000 / 8192, spacing=12000 / 8192)
BEACON_SYNC_LEN = 20  # longer acquisition preamble; beacon has no prior timing sync at all

CALLSIGN_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "  # 37 symbols


def pack_callsign(cs: str, length: int = 6) -> int:
    cs = cs.upper().ljust(length)[:length]
    v = 0
    for ch in cs:
        idx = CALLSIGN_ALPHABET.index(ch) if ch in CALLSIGN_ALPHABET else 36
        v = v * 37 + idx
    return v  # 37^6 ~= 2.57e9, fits uint32


def unpack_callsign(v: int, length: int = 6) -> str:
    chars = []
    for _ in range(length):
        chars.append(CALLSIGN_ALPHABET[v % 37])
        v //= 37
    return "".join(reversed(chars)).rstrip()


def pack_locator(loc: str) -> int:
    loc = loc.upper()
    A, B = ord(loc[0]) - ord("A"), ord(loc[1]) - ord("A")
    C, D = int(loc[2]), int(loc[3])
    return ((A * 18 + B) * 10 + C) * 10 + D  # 0..32399, fits uint16


def unpack_locator(v: int) -> str:
    D, v = v % 10, v // 10
    C, v = v % 10, v // 10
    B, v = v % 18, v // 18
    A = v % 18
    return chr(ord("A") + A) + chr(ord("A") + B) + str(C) + str(D)


def encode_beacon(callsign: str, locator: str, power_dbm: int) -> bytes:
    cs = pack_callsign(callsign)
    loc = pack_locator(locator)
    pw = max(0, min(63, int(round(power_dbm))))
    payload = struct.pack(">IHB", cs, loc, pw)  # 7 bytes = 56 bits
    crc = zlib.crc32(payload) & 0xFF
    return payload + bytes([crc])  # 8 bytes = 64 bits raw


def decode_beacon(raw: bytes):
    if len(raw) != 8:
        return None
    payload, crc = raw[:7], raw[7]
    if (zlib.crc32(payload) & 0xFF) != crc:
        return None
    cs, loc, pw = struct.unpack(">IHB", payload)
    return unpack_callsign(cs), unpack_locator(loc), pw


def simulate_beacon(callsign: str, locator: str, power_dbm: int, snr_db: float,
                     rng: np.random.Generator, fec: str = "K9", llr_mode: str = "soft"):
    """Full TX -> channel -> RX round trip for one beacon transmission."""
    c = WINK_BEACON
    fp = FEC_CONFIGS[fec]
    Kc, Gsc = fp["K"], fp["Gs"]

    raw = encode_beacon(callsign, locator, power_dbm)
    bits = bytes_to_bits(raw)
    coded = conv_encode(bits, K=Kc, Gs=Gsc)
    interleaved_len = len(coded)
    shuffled = interleave(coded)
    sync = make_sync(c.M, length=BEACON_SYNC_LEN)
    symbols = sync + bits_to_symbols_gray(shuffled, c.k)

    true_cfo = float(rng.uniform(-3, 3))
    front_pad = int(rng.integers(0, c.sps))
    tx = generate_waveform(c, symbols, cfo=true_cfo)
    lead = rng.normal(0, 1e-6, size=front_pad)
    trail = rng.normal(0, 1e-6, size=c.sps)
    full = np.concatenate([lead, tx, trail])
    rx = add_awgn(full, snr_db, rng)

    hit = find_sync(rx, c, sync)
    if hit is None:
        return None
    start, cfo_est = hit
    n_data_syms = math.ceil(interleaved_len / c.k)
    data_start = start + len(sync) * c.sps
    if data_start + n_data_syms * c.sps > len(rx):
        return None
    energies = tone_energies(rx, c, data_start, cfo_est, n_data_syms)

    sync_energies = tone_energies(rx, c, start, cfo_est, len(sync))
    sync_detected = np.argmax(sync_energies, axis=1)
    if sum(1 for a, b in zip(sync_detected, sync) if a != b) > len(sync) // 2:
        return None

    if llr_mode == "soft":
        llrs = soft_llrs_from_energies(energies, c.M, c.k)
    else:
        detected = np.argmax(energies, axis=1)
        bits_out = symbols_to_gray_bits(list(detected), c.k)
        llrs = [5.0 if b else -5.0 for b in bits_out]
    llrs = llrs[:interleaved_len]

    coded_bits_est = deinterleave(llrs, original_len=interleaved_len)
    decoded = viterbi_decode_soft(coded_bits_est, len(bits), K=Kc, Gs=Gsc)
    return decode_beacon(bits_to_bytes(decoded))
