"""WINK wideband receive / automatic TX-target support.

This is deliberately an application-facing layer around the modem, not a new
WINK waveform.  A receiver can listen to a wider audio passband and search for
multiple independent WINK carriers inside it.  Each successful decode carries
its measured audio-frequency offset so the application can target a reply at
the station's frequency automatically.

The modem itself remains profile/packet focused.  Future waveform revisions can
replace the scanner implementation without changing the UI contract.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
import numpy as np

from wink_core import Packet, bits_to_bytes, bytes_to_bits, conv_encode, deinterleave, viterbi_decode_soft
from wink_modem2 import (
    Candidate, add_awgn, find_sync, generate_waveform, make_sync,
    soft_llrs_from_energies, symbols_to_gray_bits, tone_energies,
    tx_symbols, _sync_score,
)
from wink_profiles import PROFILES, DEFAULT_FEC, DEFAULT_LLR_MODE


@dataclass(frozen=True)
class WidebandDecode:
    """One decoded station found somewhere in the receive passband."""
    packet: Packet
    profile: str
    audio_offset_hz: float
    cfo_hz: float
    confidence: float

    @property
    def tx_target_hz(self) -> float:
        """Audio offset the application should use when replying."""
        return self.audio_offset_hz


def _decode_at_offset(
    samples: np.ndarray,
    c: Candidate,
    sync: list[int],
    fec: str,
    llr_mode: str,
) -> tuple[Packet, float] | None:
    """Decode one candidate carrier.  Returns packet + sync confidence."""
    fp = __import__("wink_core").FEC_CONFIGS[fec]
    Kc, Gsc = fp["K"], fp["Gs"]
    rate = len(Gsc)

    hit = find_sync(samples, c, sync)
    if hit is None:
        return None
    start, cfo_est = hit

    # Score the sync again at the winning hypothesis.  This is useful as a
    # lightweight confidence value for the application layer.
    sync_e = tone_energies(samples, c, start, cfo_est, len(sync))
    detected = np.argmax(sync_e, axis=1)
    errors = sum(a != b for a, b in zip(detected, sync))
    confidence = 1.0 - errors / len(sync)
    if errors > len(sync) // 2:
        return None

    # We do not know packet length a priori.  Try the legal packet sizes,
    # exactly as the normal modem does, and let the packet CRC decide.
    data_start = start + len(sync) * c.sps
    max_bytes = 20 + 32
    max_raw_bits = max_bytes * 8
    max_coded = rate * (max_raw_bits + (Kc - 1))
    n_syms = math.ceil(max_coded / c.k)
    if data_start + n_syms * c.sps > len(samples):
        # A wideband recording can end before the maximum possible packet.
        # Decode whatever full symbols are available and try matching lengths.
        n_syms = (len(samples) - data_start) // c.sps
    if n_syms <= 0:
        return None

    energies = tone_energies(samples, c, data_start, cfo_est, n_syms)
    if llr_mode == "soft":
        llrs = soft_llrs_from_energies(energies, c.M, c.k)
    else:
        syms = np.argmax(energies, axis=1)
        bits = symbols_to_gray_bits(list(syms), c.k)
        llrs = [5.0 if b else -5.0 for b in bits]

    # Packet sizes are constrained by the packet format.  Test only lengths
    # whose encoded symbol count is actually present in the recording.
    for total_bytes in range(20, 20 + 32 + 1):
        raw_bits_len = total_bytes * 8
        this_coded_len = rate * (raw_bits_len + (Kc - 1))
        interleaved_len = math.ceil(this_coded_len / 8) * 8
        symbol_bits_len = math.ceil(interleaved_len / c.k) * c.k
        if symbol_bits_len > len(llrs):
            continue
        coded_llrs = deinterleave(llrs[:interleaved_len], original_len=this_coded_len)
        decoded = viterbi_decode_soft(
            coded_llrs, raw_bits_len, K=Kc, Gs=Gsc
        )
        try:
            return Packet.decode(bits_to_bytes(decoded)), confidence
        except ValueError:
            pass
    return None


def _sync_gate_score(samples: np.ndarray, c: Candidate,
                     sync: list[int]) -> float:
    """Cheap pre-screen for scan_wideband: best sync-margin score over a
    coarse start x CFO grid (4 starts x 3 CFOs = 12 energy snapshots vs
    the full find_sync search plus 33 length trials).

    Measured separation on WINK-S is enormous (true carrier ~+2e7,
    empty offset negative at -10 and -16 dB), so the default gate of 0.0
    -- positive total margin means "sync-like" -- skips ~95% of full
    decodes with no effect on results. Returns -inf if the recording is
    shorter than one sync."""
    sps = c.sps
    needed = len(sync) * sps
    if len(samples) < needed:
        return float("-inf")
    best = float("-inf")
    for start in range(0, sps, max(1, sps // 4)):
        if len(samples) - start < needed:
            continue
        for cfo in (-3.0, 0.0, 3.0):
            e = tone_energies(samples, c, start, cfo, len(sync))
            s = _sync_score(e, sync)
            if s > best:
                best = s
    return best


def scan_wideband(
    samples: np.ndarray,
    profile: str = "WINK-S",
    search_min_hz: float = -1000.0,
    search_max_hz: float = 1000.0,
    search_step_hz: float = 2.5,
    fec: str = DEFAULT_FEC,
    llr_mode: str = DEFAULT_LLR_MODE,
    gate: float = 0.0,
) -> list[WidebandDecode]:
    """Search a wide audio passband for multiple WINK signals.

    Offsets are relative to the receiver's WINK dial/base frequency.  A
    successful decode reports the offset that the UI/backend should use for
    the next TX.  Duplicate decodes of the same packet are collapsed.

    This first implementation intentionally favours correctness and a clean
    API over maximum DSP efficiency.  Once real audio is connected, the scan
    can be replaced by a coarse FFT/candidate detector without changing the
    returned contract.
    """
    if search_max_hz <= search_min_hz or search_step_hz <= 0:
        raise ValueError("invalid wideband search range")

    base = PROFILES[profile]
    results: list[WidebandDecode] = []
    seen: set[tuple[int, int, int]] = set()
    sync = make_sync(base.M)

    offsets = np.arange(search_min_hz, search_max_hz + search_step_hz / 2, search_step_hz)
    for offset in offsets:
        candidate = replace(base, base=base.base + float(offset))
        if _sync_gate_score(samples, candidate, sync) < gate:
            continue
        hit = _decode_at_offset(samples, candidate, sync, fec, llr_mode)
        if hit is None:
            continue
        packet, confidence = hit
        key = (packet.source, packet.seq, packet.dest)
        if key in seen:
            continue
        seen.add(key)
        # The offset is relative to the original dial/base frequency, not the
        # candidate's shifted internal base.
        results.append(WidebandDecode(
            packet=packet,
            profile=profile,
            audio_offset_hz=float(offset),
            cfo_hz=0.0,
            confidence=confidence,
        ))

    return sorted(results, key=lambda r: r.audio_offset_hz)


def make_multistation_recording(
    stations: list[tuple[float, Candidate, Packet]],
    rng: np.random.Generator,
    snr_db: float = -10.0,
    front_pad_samples: int = 0,
    fec: str = DEFAULT_FEC,
) -> np.ndarray:
    """Test helper: place several WINK signals at different audio offsets.

    Each tuple is (audio_offset_hz, profile_candidate, packet).  All signals
    share one recording and noise draw, approximating a busy receive passband.
    tx_symbols must use the same fec the scanner will be told to decode with,
    otherwise the coded symbol count won't match what _decode_at_offset
    expects for any packet length and the scan comes back empty.
    """
    if not stations:
        raise ValueError("at least one station required")

    waves = []
    max_len = 0
    for offset, c, packet in stations:
        symbols, _, _ = tx_symbols(c, packet, fec=fec)
        wave = generate_waveform(replace(c, base=c.base + offset), symbols, cfo=0.0)
        waves.append(wave)
        max_len = max(max_len, len(wave))

    mixed = np.zeros(front_pad_samples + max_len + 12000)
    for wave in waves:
        mixed[front_pad_samples:front_pad_samples + len(wave)] += wave
    return add_awgn(mixed, snr_db, rng)


def choose_tx_target(decodes: list[WidebandDecode], source_id: int) -> WidebandDecode | None:
    """Pick the best decoded peer for an automatic reply.

    The UI can pass the selected peer/source ID.  The returned object's
    ``tx_target_hz`` is the exact audio offset detected by the wideband RX.
    """
    matches = [d for d in decodes if d.packet.source == source_id]
    if not matches:
        return None
    return max(matches, key=lambda d: d.confidence)
