"""WINK v0.2 — generalized M-FSK physical layer.

This replaces the v0.1 bake-off's idealised "processing-gain baked in"
channel model with a real sample-domain simulation:

- actual continuous-phase audio synthesis at FS=12000 Hz
- AWGN added at the waveform-sample level (one consistent SNR definition
  for every candidate, so baud/M/spacing trade-offs are compared fairly
  instead of accidentally rewarding whichever candidate has more samples
  per symbol)
- a non-coherent (energy) matched-filter receiver, same principle as
  wink_v01/wink_modem.py but vectorised and parameterised over M
- an unknown, per-trial carrier-frequency offset (CFO) and unknown
  sub-symbol start time, recovered by a coarse search against the
  known sync sequence -- this is the "add CFO + timing error" step
  called for next in WINK_PROJECT_SUMMARY.md

Packet framing / FEC / interleaving are reused unchanged from wink_core.
"""

from __future__ import annotations
import math, random
from dataclasses import dataclass
import numpy as np

from functools import lru_cache
from wink_core import (
    Packet, conv_encode, viterbi_decode_soft, interleave, deinterleave,
    bytes_to_bits, bits_to_bytes, gray_encode, gray_decode, FEC_CONFIGS,
)
from wink_fading import FadingChannel, AWGN, apply_channel

FS = 12000
SYNC_LEN = 14

# Random CFO magnitude to test against. +-3 Hz is a deliberately
# pessimistic stand-in for combined TX/RX crystal drift + Doppler on HF;
# real crystal-referenced rigs are usually much tighter, but WINK aims
# to also work on simple MCU beacon hardware with a cheap oscillator.
CFO_RANGE_HZ = 3.0
# Unknown start time within one symbol period (receiver has no external
# time sync and must find the packet in a search window).
TIMING_SEARCH_STEPS = 8


@dataclass(frozen=True)
class Candidate:
    name: str
    M: int
    baud: float
    spacing: float
    base: float = 1000.0
    fec_rate: float = 0.5

    @property
    def sps(self) -> int:
        return round(FS / self.baud)

    @property
    def k(self) -> int:
        return int(math.log2(self.M))

    @property
    def coded_bps(self) -> float:
        return self.baud * self.k * self.fec_rate

    @property
    def occupied_hz(self) -> float:
        return self.M * self.spacing


CANDIDATES = [
    Candidate("4FSK-10Bd-5Hz", 4, 10, 5),
    Candidate("4FSK-7.5Bd-6.67Hz", 4, 7.5, 6.6666667),
    Candidate("8FSK-7.5Bd-5Hz", 8, 7.5, 5),
    Candidate("8FSK-5Bd-7.5Hz", 8, 5, 7.5),
    # v0.1's original waveform, included for a like-for-like comparison
    # against the corrected channel model.
    Candidate("8FSK-15Bd-2.5Hz (v0.1 original)", 8, 15, 2.5),
]


def make_sync(M: int, length: int = SYNC_LEN, seed: int = 1234) -> list[int]:
    rng = random.Random(seed * 1000 + M)
    return [rng.randrange(M) for _ in range(length)]


def tone_freq(c: Candidate, symbol: int) -> float:
    return c.base + symbol * c.spacing


def bits_to_symbols_gray(bits: list[int], k: int) -> list[int]:
    pad = (-len(bits)) % k
    p = bits + [0] * pad
    syms = []
    for i in range(0, len(p), k):
        v = 0
        for b in p[i:i + k]:
            v = (v << 1) | b
        syms.append(gray_encode(v))
    return syms


def symbols_to_gray_bits(symbols: list[int], k: int) -> list[int]:
    out = []
    for s in symbols:
        v = gray_decode(s)
        out.extend((v >> (k - 1 - i)) & 1 for i in range(k))
    return out


def generate_waveform(c: Candidate, symbols: list[int], cfo: float = 0.0) -> np.ndarray:
    """Continuous-phase FSK audio for a symbol sequence, with a constant
    frequency offset (cfo) applied -- models TX/RX oscillator mismatch."""
    sps = c.sps
    freqs = np.repeat([tone_freq(c, s) + cfo for s in symbols], sps)
    phase = 2 * np.pi * np.cumsum(freqs) / FS
    return np.sin(phase)


def add_awgn(x: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    p = np.mean(x ** 2)
    sigma = math.sqrt(p / (10 ** (snr_db / 10)))
    return x + rng.normal(0, sigma, size=len(x))


def _ref_matrix(c: Candidate, cfo: float) -> np.ndarray:
    """(M, sps) complex correlation templates for every tone at a given
    trial CFO hypothesis."""
    sps = c.sps
    tt = np.arange(sps) / FS
    freqs = np.array([tone_freq(c, m) + cfo for m in range(c.M)])
    return np.exp(-1j * 2 * np.pi * np.outer(freqs, tt))


def tone_energies(samples: np.ndarray, c: Candidate, start: int, cfo: float,
                   n_syms: int) -> np.ndarray:
    """(n_syms, M) non-coherent energy matrix via vectorised correlation."""
    sps = c.sps
    seg = samples[start:start + n_syms * sps]
    mat = seg.reshape(n_syms, sps)
    ref = _ref_matrix(c, cfo)
    corr = mat @ ref.conj().T
    return np.abs(corr) ** 2


@lru_cache(maxsize=None)
def _bit_groups(M: int, k: int) -> tuple:
    """For each coded-bit position j, the set of tone indices whose
    Gray-decoded value has bit j = 1, and the set where it's 0. Used to
    turn an M-ary energy vector into k soft bit LLRs (max-log approx)."""
    groups = [([], []) for _ in range(k)]
    for s in range(M):
        v = gray_decode(s)
        for j in range(k):
            bit = (v >> (k - 1 - j)) & 1
            groups[j][bit].append(s)
    return tuple((tuple(g[0]), tuple(g[1])) for g in groups)


def soft_llrs_from_energies(energies: np.ndarray, M: int, k: int) -> list[float]:
    """Max-log soft bit LLRs from per-symbol M-ary tone energies, in
    place of the placeholder fixed +-5.0 hard-decision LLRs.

    For each bit position, LLR = (best energy among tones with bit=1)
    minus (best energy among tones with bit=0) -- the standard max-log
    approximation for orthogonal-signalling soft decisions. Each symbol
    is normalised by its own noise-floor estimate (the mean of its
    lowest M/2 tone energies, i.e. the bins that shouldn't contain
    signal), so a symbol received in a noisier instant is automatically
    down-weighted relative to a cleaner one -- the actual benefit over
    the old fixed-magnitude hard-decision metric.
    """
    groups = _bit_groups(M, k)
    n_syms = energies.shape[0]
    out = []
    for i in range(n_syms):
        row = energies[i]
        noise_floor = np.mean(np.sort(row)[:max(1, M // 2)]) + 1e-12
        for j in range(k):
            zeros, ones = groups[j]
            e1 = max(row[s] for s in ones)
            e0 = max(row[s] for s in zeros)
            out.append(float((e1 - e0) / noise_floor))
    return out


def _sync_score(energies: np.ndarray, sync: list[int]) -> float:
    """How strongly the detected tones match the known sync pattern.
    Uses margin (correct-tone energy minus best competitor) so partial
    matches under noise are still discriminated, not just hard symbol
    matches."""
    total = 0.0
    for i, s in enumerate(sync):
        row = energies[i].copy()
        correct = row[s]
        row[s] = -np.inf
        total += correct - row.max()
    return total


def find_sync(samples: np.ndarray, c: Candidate, sync: list[int]) -> tuple[int, float] | None:
    """Coarse search over unknown start offset and CFO. Returns the
    (start_sample, cfo_hz) hypothesis with the strongest sync-pattern
    match, or None if the search window is too short."""
    sps = c.sps
    needed = len(sync) * sps
    if len(samples) < needed:
        return None

    step = max(1, sps // TIMING_SEARCH_STEPS)
    starts = range(0, sps, step)
    cfos = np.linspace(-CFO_RANGE_HZ, CFO_RANGE_HZ, 13)

    best = None
    best_score = -np.inf
    for start in starts:
        if len(samples) - start < needed:
            continue
        for cfo in cfos:
            energies = tone_energies(samples, c, start, float(cfo), len(sync))
            score = _sync_score(energies, sync)
            if score > best_score:
                best_score = score
                best = (start, float(cfo))
    return best


def _rx_llrs_one_shot(c: Candidate, rx: np.ndarray, sync: list[int], interleaved_len: int,
                       llr_mode: str) -> list[float] | None:
    """One repeat's soft/hard LLRs for a packet of *known* interleaved
    length, or None if sync search fails. Used by diversity combining,
    where the receiver already knows what length it's listening for
    (e.g. after the first successful CRC, or by convention for a fixed
    retransmission)."""
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
        bits = symbols_to_gray_bits(list(detected), c.k)
        llrs = [5.0 if b else -5.0 for b in bits]
    return llrs[:interleaved_len]


def simulate_packet_diversity(c: Candidate, packet: Packet, snr_db: float,
                               rng: np.random.Generator, n_repeats: int = 2,
                               fec: str = "K7", llr_mode: str = "hard",
                               channel: FadingChannel = AWGN) -> Packet | None:
    """Send the same packet n_repeats times over independent channel
    draws (independent CFO, timing, and noise -- as if retransmitted a
    few seconds apart) and soft-combine the LLRs before one Viterbi
    decode. This is real diversity gain (~10*log10(n_repeats) dB in the
    ideal case), at the direct cost of n_repeats x the time-on-air.

    channel: if fading is active, each repeat gets an independent fade
    realization from ``rng``, i.e. the fading decorrelates between
    repeats -- that is the diversity case this feature exists for.
    """
    fp = FEC_CONFIGS[fec]
    Kc, Gsc = fp["K"], fp["Gs"]
    raw = packet.encode()
    bits = bytes_to_bits(raw)
    coded = conv_encode(bits, K=Kc, Gs=Gsc)
    interleaved_len = len(coded)
    shuffled = interleave(coded)
    sync = make_sync(c.M)
    symbols = sync + bits_to_symbols_gray(shuffled, c.k)

    combined = np.zeros(interleaved_len)
    any_hit = False
    for r in range(n_repeats):
        tx = generate_waveform(c, symbols, cfo=float(rng.uniform(-3, 3)))
        front_pad = int(rng.integers(0, c.sps))
        lead = rng.normal(0, 1e-6, size=front_pad)
        trail = rng.normal(0, 1e-6, size=c.sps)
        full = np.concatenate([lead, tx, trail])
        faded = apply_channel(full, rng, channel)
        rx = add_awgn(faded, snr_db, rng)
        llrs = _rx_llrs_one_shot(c, rx, sync, interleaved_len, llr_mode)
        if llrs is not None:
            combined += np.array(llrs)
            any_hit = True

    if not any_hit:
        return None
    coded_bits_est = deinterleave(list(combined), original_len=interleaved_len)
    decoded = viterbi_decode_soft(coded_bits_est, len(bits), K=Kc, Gs=Gsc)
    raw_out = bits_to_bytes(decoded)
    try:
        return Packet.decode(raw_out)
    except ValueError:
        return None


def tx_symbols(c: Candidate, packet: Packet, fec: str = "K7") -> tuple[list[int], int, list[int]]:
    raw = packet.encode()
    bits = bytes_to_bits(raw)
    fp = FEC_CONFIGS[fec]
    coded = conv_encode(bits, K=fp["K"], Gs=fp["Gs"])
    shuffled = interleave(coded)
    sync = make_sync(c.M)
    return sync + bits_to_symbols_gray(shuffled, c.k), len(coded), sync


def simulate_packet(c: Candidate, packet: Packet, snr_db: float, rng: np.random.Generator,
                     true_cfo: float, front_pad_samples: int,
                     fec: str = "K7", llr_mode: str = "hard",
                     channel: FadingChannel = AWGN) -> Packet | None:
    """Full TX -> channel -> RX round trip for one packet. Returns the
    decoded Packet, or None on sync/CRC failure.

    fec: K7/K9 (rate 1/2) or K9_1_3 (rate 1/3, ~1.5dB more gain at 1.5x airtime).
    llr_mode: hard (fixed +-5) or soft (max-log from tone energies).
    channel: FadingChannel (see wink_fading.py). Default AWGN; pass a
    Rayleigh/Rician/two-path model to see what the AWGN-only thresholds
    actually cost on an HF channel. SNR is referenced to average
    received power, so deep fades still throw symbols at high SNR.
    """
    fp = FEC_CONFIGS[fec]
    Kc, Gsc = fp["K"], fp["Gs"]
    rate = len(Gsc)
    symbols, coded_len, sync = tx_symbols(c, packet, fec=fec)
    tx = generate_waveform(c, symbols, cfo=true_cfo)
    lead = rng.normal(0, 1e-6, size=front_pad_samples)
    trail = rng.normal(0, 1e-6, size=c.sps)
    full = np.concatenate([lead, tx, trail])
    faded = apply_channel(full, rng, channel)
    rx = add_awgn(faded, snr_db, rng)

    hit = find_sync(rx, c, sync)
    if hit is None:
        return None
    start, cfo_est = hit

    n_data_syms = len(symbols) - len(sync)
    data_start = start + len(sync) * c.sps
    if data_start + n_data_syms * c.sps > len(rx):
        return None
    energies = tone_energies(rx, c, data_start, cfo_est, n_data_syms)
    detected = np.argmax(energies, axis=1)

    sync_energies = tone_energies(rx, c, start, cfo_est, len(sync))
    sync_detected = np.argmax(sync_energies, axis=1)
    sync_errors = sum(1 for a, b in zip(sync_detected, sync) if a != b)
    if sync_errors > len(sync) // 2:
        return None

    packed_bits = symbols_to_gray_bits(list(detected), c.k)
    if llr_mode == "soft":
        packed_llrs = soft_llrs_from_energies(energies, c.M, c.k)
    else:
        packed_llrs = [5.0 if b else -5.0 for b in packed_bits]

    # Direct length calc from known packet (no trial-and-error over 33 lengths)
    raw = packet.encode()
    raw_bits_len = len(raw) * 8
    this_coded_len = rate * (raw_bits_len + (Kc - 1))
    interleaved_len = math.ceil(this_coded_len / 8) * 8
    if interleaved_len > len(packed_llrs):
        return None
    padded_llrs = packed_llrs[:interleaved_len]
    coded_llrs = deinterleave(padded_llrs, original_len=this_coded_len)
    decoded = viterbi_decode_soft(coded_llrs, raw_bits_len, K=Kc, Gs=Gsc)
    raw_decoded = bits_to_bytes(decoded)
    try:
        return Packet.decode(raw_decoded)
    except ValueError:
        return None
