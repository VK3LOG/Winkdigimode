#!/usr/bin/env python3
"""WINK v0.2 — real audio-loopback validation.

Goes one step past AWGN-only simulation: synthesises the exact WINK
audio that a TX would put on the wire (16-bit PCM at FS=12000, padded
to -6 dBFS headroom), plays it out through the system audio device,
records it back, and runs the real sample-domain decoder on the
recording -- proving the sample-rate, level, sync search and CFO
recovery assumptions against actual hardware.

Two modes:

    python3 audio_loopback.py --mock -20
        Deterministic in-process channel (the full audio->PCM->decode
        pipeline with AWGN at the requested SNR). No audio device
        needed; verifies the toolchain.

    python3 audio_loopback.py --profile WINK-S --device "default"
        Real loopback. Requires an actual audio path from the device's
        output back to its input (cable, or a PulseAudio/PipeWire
        "Monitor of ..." sink). If the recorded level is essentially
        silence, the script says so explicitly instead of guessing.

    python3 audio_loopback.py --list-devices

Report includes: decoded packet (or failure), measured carrier offset
(recovered CFO), recovered symbol timing, and an SNR estimate taken
from the quiet lead-in vs the signal region -- an honest proxy, not a
built-in truth like the simulator gives.
"""
import argparse
import sys
import wave

import numpy as np

from wink_core import Packet, callsign_id
from wink_modem2 import (
    FS, Candidate, generate_waveform, find_sync, make_sync,
)
from wink_profiles import PROFILES, DEFAULT_FEC, DEFAULT_LLR_MODE
from wink_wideband import _decode_at_offset


def build_audio(profile_name: str, payload: bytes, fec: str = DEFAULT_FEC,
                tx_cfo: float = 0.0) -> tuple[Candidate, np.ndarray, list[int]]:
    """Synthesise the exact passband audio for one WINK packet.

    Uses the same generators as the simulator (interleave/FEC/sync), so
    the loopback exercises the real TX side, not a special test path.
    """
    from wink_modem2 import tx_symbols
    c = PROFILES[profile_name]
    pkt = Packet(1, callsign_id("VK3TEST"), callsign_id("VK5TEST"), 1, payload)
    symbols, _coded_len, _sync = tx_symbols(c, pkt, fec=fec)
    audio = generate_waveform(c, symbols, cfo=tx_cfo)
    # Pad with a short quiet lead-in (the recorder needs time to start)
    # and a quiet tail, then normalise to -6 dBFS with headroom.
    lead = np.zeros(c.sps * 2)
    tail = np.zeros(c.sps * 2)
    full = np.concatenate([lead, audio, tail])
    peak = np.max(np.abs(full)) or 1.0
    return c, (full / peak) * 0.5, symbols


def to_pcm16_wav(path: str, floats: np.ndarray, rate: int = FS) -> None:
    pcm = np.clip(floats, -1.0, 1.0)
    pcm16 = (pcm * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.tobytes())


def read_pcm16_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        nch = w.getnchannels()
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if nch > 1:
        frames = frames.reshape(-1, nch).mean(axis=1)
    return frames.astype(np.float64) / 32767.0, rate


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """FFT-based bandlimited resample (fast, no extra deps)."""
    x = np.asarray(x, dtype=np.float64)
    if sr_in == sr_out:
        return x
    n_in = len(x)
    n_out = int(round(n_in * sr_out / sr_in))
    if n_in == 0 or n_out == 0:
        return np.zeros(max(n_out, 0))
    X = np.fft.rfft(x)
    # Map bins: keep content up to min Nyquist, zero-pad or truncate.
    n_bins_out = n_out // 2 + 1
    Y = np.zeros(n_bins_out, dtype=complex)
    n_copy = min(len(X), n_bins_out)
    Y[:n_copy] = X[:n_copy]
    return np.fft.irfft(Y, n=n_out)


def find_tx_window(rec: np.ndarray, c: Candidate, sync: list[int],
                   fec: str, llr_mode: str,
                   coarse_fraction: float = 0.5, max_tries: int = 96):
    """Locate a WINK burst anywhere in a (possibly long) recording.

    find_sync only searches one symbol period from sample 0, so for a
    loopback capture the burst's absolute position is unknown.  Slide a
    coarse window (half a symbol per step) across the recording and run
    the real modem decode from each; return (decode, window_start) on
    the first window that yields a packet.  The window that matches has
    the sync within one symbol of its first sample, so the normal modem
    search then takes over exactly as it would on-air.
    """
    step = max(1, int(c.sps * coarse_fraction))
    needed = len(sync) * c.sps
    for k in range(int(max_tries)):
        o = k * step
        if o + needed > len(rec):
            break
        hit = _decode_at_offset(rec[o:], c, sync, fec, llr_mode)
        if hit is not None:
            return hit, o
    return None, None


def decode_from_recording(rec: np.ndarray, profile_name: str,
                          fec: str, llr_mode: str) -> dict:
    """Decode one capture (burst at any offset); return result + metrics."""
    c = PROFILES[profile_name]
    sync = make_sync(c.M)
    dec, o = find_tx_window(rec, c, sync, fec, llr_mode)
    if dec is None:
        return {"ok": False, "reason": "no sync / no packet found in capture"}

    pkt, confidence, cfo_est = dec
    # Absolute timing still needs the normal sync search on the matched
    # window; the CFO comes straight from the decode (no re-search).
    inner = find_sync(rec[o:], c, sync)
    inner_start = inner[0] if inner is not None else 0
    start = o + inner_start

    # SNR estimate: quiet lead-in vs data region.
    ns = int(c.sps)
    noise_region = rec[max(0, start - 3000):max(1, start - ns // 2)]
    data_region = rec[start: min(len(rec), start + ns * 64)]
    sig_rms = np.sqrt(np.mean(data_region ** 2)) if len(data_region) else 0.0
    noise_rms = np.sqrt(np.mean(noise_region ** 2)) if len(noise_region) else 0.0
    est_snr = None
    if noise_rms > 0 and sig_rms > noise_rms:
        est_snr = 10.0 * np.log10((sig_rms ** 2 - noise_rms ** 2) / noise_rms ** 2)
    return {
        "ok": True,
        "payload": bytes(pkt.payload),
        "kind": pkt.kind,
        "source": pkt.source,
        "dest": pkt.dest,
        "seq": pkt.seq,
        "confidence": float(confidence),
        "cfo_hz": float(cfo_est),
        "start_sample": int(start),
        "est_snr_db": est_snr,
    }


def run_mock(args) -> int:
    import math
    payload = args.payload.encode()
    c, audio, _ = build_audio(args.profile, payload, fec=args.fec)
    rng = np.random.default_rng(args.seed)
    true_snr = args.mock
    # Reference noise to signal-only power (exclude the quiet lead/tail),
    # otherwise the silence dilutes the mean and the requested SNR is a lie.
    lead = c.sps * 2
    sig = audio[lead:-lead] if lead > 0 else audio
    p = float(np.mean(sig ** 2)) or 1e-12
    sigma = math.sqrt(p / (10.0 ** (true_snr / 10.0)))
    rx = audio + rng.normal(0.0, sigma, size=len(audio))
    report = decode_from_recording(rx, args.profile, args.fec, args.llr)
    report["mock_snr_db"] = true_snr
    print(f"mock channel @ SNR {true_snr} dB, profile {args.profile}, fec {args.fec}")
    dump_report(report)
    to_pcm16_wav(args.out_wav, rx)
    print(f"wrote recovered audio -> {args.out_wav}")
    return 0 if report["ok"] else 1


def run_loopback(args) -> int:
    import sounddevice as sd

    if args.list_devices:
        print(sd.query_devices())
        return 0

    dev = args.device or sd.default.device
    info = sd.query_devices(dev, "output")
    dev_rate = int(info["default_samplerate"])
    channels = (args.in_ch, args.out_ch)

    c, audio, _ = build_audio(args.profile, args.payload.encode(), fec=args.fec)
    audio_up = resample(audio, FS, dev_rate)
    duration = len(audio_up) / dev_rate + 0.5  # +0.5 s settle/settle tail

    print(f"playing {len(audio_up)/dev_rate:.2f}s on device {dev} "
          f"(rate {dev_rate} Hz) and recording {duration:.2f}s...", flush=True)
    recorded = sd.playrec(audio_up,
                          samplerate=dev_rate,
                          channels=channels,
                          device=dev,
                          blocking=True)
    rec = recorded[:, args.in_ch - 1].astype(np.float64)
    # optionally invert/scale by measured max; keep raw levels for the report
    rec_down = resample(rec, dev_rate, FS)

    ok = True
    max_abs = np.max(np.abs(rec_down)) if len(rec_down) else 0.0
    if max_abs < 5e-3:
        ok = False
        print("!! recorded level is effectively silence.")
        print("   No audio path from this device's output back to its",
              "input exists (loopback cable, or a PulseAudio/PipeWire",
              "'Monitor of ...' sink routed to the input).")
        print(f"   peak = {max_abs:.6f} (need > ~5e-3). Try --list-devices",
              "and pick a loopback/monitor pair.")
    report = decode_from_recording(rec_down, args.profile, args.fec, args.llr)
    report["peak_rms"] = float(np.sqrt(np.mean(rec_down ** 2)))
    dump_report(report)
    to_pcm16_wav(args.out_wav, rec_down)
    print(f"wrote recorded audio -> {args.out_wav}")
    return 0 if (ok and report["ok"]) else 1


def dump_report(r: dict) -> None:
    print(f"  decoded:     {'YES' if r.get('ok') else 'NO'}"
          + ("" if r.get("ok") else f"  ({r.get('reason')})"))
    if r.get("ok"):
        print(f"  payload:     {r['payload']!r}")
        print(f"  kind/seq:    {r['kind']}/{r['seq']}   src={r['source']:#x} dest={r['dest']:#x}")
        print(f"  sync conf:   {r.get('confidence'):.2f}")
    print(f"  measured CFO:{r.get('cfo_hz', float('nan')):+7.2f} Hz")
    print(f"  timing:      start sample {r.get('start_sample', 'n/a')}")
    snr = r.get("est_snr_db")
    print(f"  est SNR:     {snr:+.1f} dB" if snr is not None else "  est SNR:     n/a (silence)")
    peak = r.get("peak_rms")
    if peak is not None:
        print(f"  recorded RMS:{peak*32767:8.1f} / 32767 ({20*np.log10(peak+1e-12):+.0f} dBFS)")


def main(argv=None):
    p = argparse.ArgumentParser(description="WINK audio loopback validation")
    p.add_argument("--profile", choices=sorted(PROFILES), default="WINK-S")
    p.add_argument("--payload", default="hello WINK")
    p.add_argument("--fec", choices=("K7", "K9", "K9_1_3"), default=DEFAULT_FEC)
    p.add_argument("--llr", choices=("soft", "hard"), default=DEFAULT_LLR_MODE)
    p.add_argument("--mock", type=float, default=None, metavar="SNR_DB",
                   help="run the in-process channel at this SNR instead of audio")
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--device", default=None, help="sounddevice device (default or 'in,out')")
    p.add_argument("--in-ch", type=int, default=1, help="recording channel")
    p.add_argument("--out-ch", type=int, default=1, help="playback channel")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--out-wav", default=None, help="where to stash the captured/mock audio")
    args = p.parse_args(argv)
    import os
    if args.out_wav is None:
        args.out_wav = os.path.join("wink_out", f"loopback_{args.profile}.wav")
        os.makedirs(os.path.dirname(args.out_wav) or ".", exist_ok=True)
    if args.list_devices:
        try:
            import sounddevice as sd
        except Exception as e:
            print(f"sounddevice unavailable: {e}")
            return 2
        print(sd.query_devices())
        return 0
    try:
        if args.mock is not None and args.device:
            print("ignoring --device in mock mode")
        if args.mock is not None:
            return run_mock(args)
        return run_loopback(args)
    except ImportError as e:
        print(f"audio backend not available: {e}")
        print("(install with: pip install --break-system-packages sounddevice)")
        return 2


if __name__ == "__main__":
    sys.exit(main())