"""Faithful FT8 physical-layer simulator for head-to-head comparison.

Implements the real WSJT-X FT8 frame (not an approximation):
  * 79 symbols x 0.16 s = 12.64 s, 8-FSK, 6.25 Hz tone spacing (50 Hz BW)
  * sync: 7x7 Costas [3,1,4,0,6,5,2] at symbol positions 0-6, 36-42, 72-78
  * data: 58 symbols x 3 bits (binary, MSB first) = 174-bit LDPC(174,91)
    codeword carrying 77 message bits + 14-bit CRC (poly 0x6757)
  * LDPC tables (G, sparse H) taken from WSJT-X lib/ft8 and validated:
    random codewords satisfy all 83 checks (see ft8_ldpc_tables.py)

Deliberate, documented approximations vs WSJT-X:
  * continuous-phase FSK instead of GFSK (same tone set/spacing; GFSK is
    slightly narrower but sensitivity is essentially identical)
  * min-sum belief-propagation LDPC decoder (same algorithm family as
    WSJT-X bpdecode174_91; no a-priori message support, no multi-pass)
  * unknown start within one symbol + unknown CFO +-3 Hz, recovered by
    Costas search -- the same impairment model as wink_modem2, so the
    comparison is apples-to-apples on channel handling

simulate_ft8() mirrors wink_modem2.simulate_packet(): full TX ->
channel -> RX round trip returning the decoded 77-bit message or None.
SNR is referenced the same way (mean power over the burst), and
wink_fading.apply_channel() is reused so AWGN vs Rayleigh runs use the
identical channel realizations framework as WINK.
"""

from __future__ import annotations
import math
import numpy as np

from wink_fading import FadingChannel, AWGN, apply_channel
from ft8_ldpc_tables import G as _G, CHECKS as _CHECKS, CRC14_POLY as _POLY

FS = 12000
SYM_S = 0.16
SPS = int(round(FS * SYM_S))          # 1920 samples per symbol
N_SYM = 79
TONE_SPACING = 6.25
COSTAS = [3, 1, 4, 0, 6, 5, 2]
SYNC_POS = set(list(range(0, 7)) + list(range(36, 43)) + list(range(72, 79)))
DATA_POS = [i for i in range(N_SYM) if i not in SYNC_POS]  # 58 symbols
assert len(DATA_POS) == 58
_SYNC_ORDER = [0, 1, 2, 3, 4, 5, 6,
               36, 37, 38, 39, 40, 41, 42,
               72, 73, 74, 75, 76, 77, 78]
SYNC_TONE = {s: COSTAS[i % 7] for i, s in enumerate(_SYNC_ORDER)}

_G = np.array(_G, dtype=np.int8)
# var -> checks adjacency for BP
_VAR_CHECKS: list[list[int]] = [[] for _ in range(174)]
for _j, _members in enumerate(_CHECKS):
    for _b in _members:
        _VAR_CHECKS[_b].append(_j)


def crc14(msg77: np.ndarray) -> np.ndarray:
    """14-bit CRC (poly 0x6757) over 77 message bits, WSJT-X get_crc14."""
    p = np.array(_POLY, dtype=np.int8)
    m = np.concatenate([np.array(msg77, dtype=np.int8), np.zeros(14, dtype=np.int8)])
    for i in range(77):
        if m[i]:
            m[i:i + 15] ^= p
    return m[77:91]


def encode77(msg77: np.ndarray) -> np.ndarray:
    """77 message bits -> 174-bit codeword in transmitted order."""
    msg = np.concatenate([np.array(msg77, dtype=np.int8), crc14(msg77)])
    p = (_G.dot(msg)) % 2
    return np.concatenate([msg, p]).astype(np.int8)


def _bits_to_tones(codeword: np.ndarray) -> list[int]:
    return [(int(codeword[3 * i]) << 2) | (int(codeword[3 * i + 1]) << 1) | int(codeword[3 * i + 2])
            for i in range(58)]


def modulate(codeword: np.ndarray, cfo: float = 0.0, base_hz: float = 1000.0) -> np.ndarray:
    """Continuous-phase 8-FSK waveform for one FT8 frame."""
    data_tones = _bits_to_tones(codeword)
    tones = []
    di = 0
    for s in range(N_SYM):
        if s in SYNC_POS:
            tones.append(SYNC_TONE[s])
        else:
            tones.append(data_tones[di])
            di += 1
    out = np.zeros(N_SYM * SPS)
    phase = 0.0
    for s, tone in enumerate(tones):
        f = base_hz + tone * TONE_SPACING + cfo
        t = np.arange(SPS) / FS
        out[s * SPS:(s + 1) * SPS] = np.cos(2 * np.pi * f * t + phase)
        phase = (phase + 2 * np.pi * f * SPS / FS) % (2 * np.pi)
    peak = np.max(np.abs(out)) or 1.0
    return out / peak


def _ref_matrix(cfo: float, base_hz: float = 1000.0) -> np.ndarray:
    """(8, SPS) complex correlation templates (quadrature, non-coherent --
    same detector structure as wink_modem2.tone_energies)."""
    t = np.arange(SPS) / FS
    freqs = np.array([base_hz + k * TONE_SPACING + cfo for k in range(8)])
    return np.exp(-1j * 2 * np.pi * np.outer(freqs, t))


def tone_energies(rx: np.ndarray, start: int, cfo: float, n_syms: int,
                  base_hz: float = 1000.0) -> np.ndarray:
    seg = rx[start:start + n_syms * SPS]
    mat = seg.reshape(n_syms, SPS)
    ref = _ref_matrix(cfo, base_hz)
    corr = mat @ ref.conj().T
    return np.abs(corr) ** 2


def find_sync(rx: np.ndarray, base_hz: float = 1000.0,
              cfo_range: float = 3.0) -> tuple[int, float] | None:
    """Coarse search over start (one symbol) and CFO for the Costas sync."""
    needed = N_SYM * SPS
    if len(rx) < needed:
        return None
    step = max(1, SPS // 8)
    cfos = np.linspace(-cfo_range, cfo_range, 13)
    sync_rows = sorted(SYNC_POS)
    sync_tones = [SYNC_TONE[s] for s in sync_rows]
    best, best_score = None, -np.inf
    for start in range(0, SPS, step):
        if len(rx) - start < needed:
            continue
        for cfo in cfos:
            e = tone_energies(rx, start, float(cfo), N_SYM, base_hz)
            score = 0.0
            for r, s_pos, tone in zip(range(len(sync_rows)), sync_rows, sync_tones):
                row = e[s_pos].copy()
                correct = row[tone]
                row[tone] = -np.inf
                score += correct - row.max()
            if score > best_score:
                best_score = score
                best = (start, float(cfo))
    return best


def soft_llrs(e58: np.ndarray) -> np.ndarray:
    """Max-log bit LLRs (binary MSB-first) from 58 data-symbol energies.

    Same structure as wink_modem2.soft_llrs_from_energies: per-bit gap
    between the best tone with bit=0 and the best with bit=1, normalised
    by the symbol's own noise-floor estimate (mean of the 4 lowest tone
    energies -- the bins that shouldn't contain signal), so noisy symbols
    are down-weighted. Sign convention matches the BP decoder below
    (LLR = log(P0/P1), positive favors 0).
    """
    llrs = np.zeros(174)
    for i in range(58):
        row = e58[i]
        noise = float(np.mean(np.sort(row)[:4])) + 1e-12
        for b in range(3):
            mask0 = np.array([(t >> (2 - b)) & 1 == 0 for t in range(8)])
            best0 = row[mask0].max()
            best1 = row[~mask0].max()
            llrs[3 * i + b] = (best0 - best1) / noise
    return llrs


def bp_decode(llr: np.ndarray, max_iter: int = 40) -> np.ndarray | None:
    """Min-sum belief propagation; returns 91 info bits or None."""
    n, m = 174, 83
    # messages var->check, indexed [var][k]
    v2c = [[float(llr[v]) for _ in _VAR_CHECKS[v]] for v in range(n)]
    for _ in range(max_iter):
        # check -> var
        c2v: dict[tuple[int, int], float] = {}
        for j, members in enumerate(_CHECKS):
            vals = []
            for v in members:
                k = _VAR_CHECKS[v].index(j)
                vals.append(v2c[v][k])
            for vi, v in enumerate(members):
                others = [vals[t] for t in range(len(vals)) if t != vi]
                sgn = 1.0
                mn = np.inf
                for x in others:
                    sgn *= 1.0 if x >= 0 else -1.0
                    mn = min(mn, abs(x))
                c2v[(j, v)] = sgn * (mn if np.isfinite(mn) else 0.0)
        # hard decision
        est = np.zeros(n, dtype=np.int8)
        for v in range(n):
            tot = float(llr[v]) + sum(c2v[(j, v)] for j in _VAR_CHECKS[v])
            est[v] = 0 if tot >= 0 else 1
        if all(sum(est[i] for i in members) % 2 == 0 for members in _CHECKS):
            return est[:91]
        # var -> check update
        for v in range(n):
            for k, j in enumerate(_VAR_CHECKS[v]):
                v2c[v][k] = float(llr[v]) + sum(c2v[(jj, v)] for jj in _VAR_CHECKS[v] if jj != j)
    return None


def check_crc(msg91: np.ndarray) -> bool:
    m = np.array(msg91[:77], dtype=np.int8)
    return bool(np.array_equal(crc14(m), np.array(msg91[77:91], dtype=np.int8)))


def simulate_ft8(msg77: np.ndarray, snr_db: float, rng: np.random.Generator,
                 true_cfo: float, front_pad_samples: int,
                 channel: FadingChannel = AWGN,
                 base_hz: float = 1000.0) -> np.ndarray | None:
    """Full TX -> channel -> RX round trip for one FT8 frame.

    Returns the decoded 91 info bits (77 msg + 14 CRC) on CRC pass,
    else None. Mirrors wink_modem2.simulate_packet semantics.
    """
    cw = encode77(np.array(msg77, dtype=np.int8))
    tx = modulate(cw, cfo=true_cfo, base_hz=base_hz)
    lead = rng.normal(0, 1e-6, size=front_pad_samples)
    trail = rng.normal(0, 1e-6, size=SPS)
    full = np.concatenate([lead, tx, trail])
    faded = apply_channel(full, rng, channel)
    p = float(np.mean(faded ** 2)) or 1e-12
    sigma = math.sqrt(p / (10.0 ** (snr_db / 10.0)))
    rx = faded + rng.normal(0.0, sigma, size=len(faded))

    hit = find_sync(rx, base_hz)
    if hit is None:
        return None
    start, cfo_est = hit
    if start + N_SYM * SPS > len(rx):
        return None
    e = tone_energies(rx, start, cfo_est, N_SYM, base_hz)
    # verify sync hard (gate like WINK's sync check)
    sync_rows = sorted(SYNC_POS)
    sync_tones = [SYNC_TONE[s] for s in sync_rows]
    errs = sum(int(np.argmax(e[s]) != t) for s, t in zip(sync_rows, sync_tones))
    if errs > len(sync_rows) // 2:
        return None
    e58 = e[DATA_POS]
    llr = soft_llrs(e58)
    msg91 = bp_decode(llr)
    if msg91 is None or not check_crc(msg91):
        return None
    return msg91


def frame_info() -> dict:
    return {
        "symbols": N_SYM, "symbol_s": SYM_S, "time_on_air_s": N_SYM * SYM_S,
        "bandwidth_hz": 8 * TONE_SPACING, "info_bits": 91, "message_bits": 77,
        "throughput_bps": 91 / (N_SYM * SYM_S),
    }
