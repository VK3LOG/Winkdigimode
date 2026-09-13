"""WINK engine for the GUI: real modem trials in background threads.

Every function here runs the actual validated modem (beacon round-trip,
wideband scan, adaptive link trial) -- no mock data. Work runs in a
daemon thread via run_async(); results are delivered on the GTK main
loop through GLib.idle_add, so pages never block the UI even though a
single trial takes seconds.
"""
import threading
import time

import numpy as np
from gi.repository import GLib

from wink_core import Packet, callsign_id
from wink_modem2 import simulate_packet
from wink_beacon import simulate_beacon
from wink_profiles import (
    PROFILES, ORDER, THRESHOLDS, THRESHOLDS_FADING, THRESHOLDS_STRONG,
    recommend_profile, DEFAULT_FEC, DEFAULT_LLR_MODE, STRONG_FEC,
)
from wink_wideband import make_multistation_recording, scan_wideband
from wink_fading import FadingChannel


def run_async(work, on_done):
    """Run work() in a daemon thread; call on_done(result) on GTK loop."""
    def _run():
        try:
            result = work()
        except Exception as exc:  # never kill the UI thread silently
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        GLib.idle_add(lambda: on_done(result) or False)
    threading.Thread(target=_run, daemon=True).start()


def beacon_trial(snr_db: float, fec: str = DEFAULT_FEC,
                 seed: int | None = None) -> dict:
    """One real WINK-Beacon TX -> channel -> RX round trip."""
    t0 = time.time()
    rng = np.random.default_rng(seed if seed is not None else int(time.time() * 1000) & 0xFFFFFFFF)
    try:
        hit = simulate_beacon("VK3LOG", "QF22", 27, float(snr_db), rng,
                              fec=fec, llr_mode="soft")
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": time.time() - t0}
    if hit is None:
        return {"ok": False, "reason": "no decode", "snr_db": snr_db,
                "fec": fec, "elapsed_s": time.time() - t0}
    callsign, locator, power = hit
    return {"ok": True, "callsign": callsign, "locator": locator,
            "power_dbm": power, "snr_db": snr_db, "fec": fec,
            "elapsed_s": time.time() - t0}


def wideband_trial(snr_db: float = -10.0, profile: str = "WINK-S",
                   fec: str = DEFAULT_FEC, seed: int = 7) -> dict:
    """Build a real 2-station passband and scan it with the real decoder."""
    t0 = time.time()
    try:
        c = PROFILES[profile]
        rng = np.random.default_rng(seed)
        stations = [
            (-120.0, c, Packet(1, callsign_id("VK2XYZ"), callsign_id("VK3LOG"), 4, b"cq cq")),
            (180.0, c, Packet(1, callsign_id("VK4QRP"), callsign_id("VK3LOG"), 9, b"qrp here")),
        ]
        samples = make_multistation_recording(stations, rng, snr_db=snr_db, fec=fec)
        decodes = scan_wideband(samples, profile=profile,
                                search_min_hz=-200.0, search_max_hz=200.0,
                                search_step_hz=5.0, fec=fec, llr_mode="soft")
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": time.time() - t0}
    return {"ok": True, "stations": [
        {"payload": bytes(d.packet.payload),
         "source": d.packet.source, "seq": d.packet.seq,
         "offset_hz": d.audio_offset_hz, "cfo_hz": d.cfo_hz,
         "confidence": float(d.confidence)} for d in decodes],
        "snr_db": snr_db, "profile": profile,
        "elapsed_s": time.time() - t0}


def link_trial(profile: str, snr_db: float, channel: str = "awgn",
               fec: str = DEFAULT_FEC, seed: int | None = None) -> dict:
    """One real adaptive-link exchange + the genuine recommendation."""
    t0 = time.time()
    try:
        c = PROFILES[profile]
        pkt = Packet(1, callsign_id("VK3ABC"), callsign_id("VK3XYZ"), 1, b"gui link test")
        rng = np.random.default_rng(seed if seed is not None else int(time.time() * 1000) & 0xFFFFFFFF)
        ch = FadingChannel(kind="rayleigh", fd_hz=1.0) if channel == "fading" \
            else FadingChannel(kind="awgn")
        rx = simulate_packet(c, pkt, float(snr_db), rng,
                             float(rng.uniform(-3, 3)),
                             int(rng.integers(0, c.sps)),
                             fec=fec, llr_mode="soft", channel=ch)
        decoded = rx is not None and rx.payload == pkt.payload
        recommended = recommend_profile(float(snr_db), profile, channel=channel)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": time.time() - t0}
    return {"ok": True, "decoded": decoded, "recommended": recommended,
            "snr_db": snr_db, "profile": profile, "channel": channel,
            "fec": fec, "elapsed_s": time.time() - t0}


def send_text(text: str, source_id: int, dest_id: int, profile: str,
              snr_db: float, channel: str = "awgn", fec: str = DEFAULT_FEC,
              seq: int = 1, seed: int | None = None) -> dict:
    """Keyboard-to-keyboard send: TEXT packet through the real channel.

    Returns decoded text (or a miss) plus the genuine profile
    recommendation, so the GUI's TX box behaves like an on-air QSO --
    including honest failures at low SNR.
    """
    t0 = time.time()
    try:
        c = PROFILES[profile]
        pkt = Packet(1, source_id, dest_id, seq, text.encode()[:32])
        rng = np.random.default_rng(seed if seed is not None else int(time.time() * 1000) & 0xFFFFFFFF)
        ch = FadingChannel(kind="rayleigh", fd_hz=1.0) if channel == "fading" \
            else FadingChannel(kind="awgn")
        rx = simulate_packet(c, pkt, float(snr_db), rng,
                             float(rng.uniform(-3, 3)),
                             int(rng.integers(0, c.sps)),
                             fec=fec, llr_mode="soft", channel=ch)
        recommended = recommend_profile(float(snr_db), profile, channel=channel)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": time.time() - t0}
    if rx is None:
        return {"ok": True, "decoded": False, "recommended": recommended,
                "snr_db": snr_db, "profile": profile, "channel": channel,
                "fec": fec, "elapsed_s": time.time() - t0}
    try:
        text_out = bytes(rx.payload).decode(errors="replace")
    except Exception:
        text_out = repr(bytes(rx.payload))
    return {"ok": True, "decoded": True, "text": text_out,
            "source": rx.source, "seq": rx.seq,
            "recommended": recommended, "snr_db": snr_db,
            "profile": profile, "channel": channel, "fec": fec,
            "elapsed_s": time.time() - t0}


def tx_audio(text: str, source_id: int, dest_id: int, profile: str,
             fec: str = DEFAULT_FEC, seq: int = 1,
             device_out=None, tx_cfo: float = 0.0) -> dict:
    """Real soundcard TX: synthesise the packet audio and play it out
    the selected output device. Returns duration info; the RF/air path
    is the operator's radio, not simulated -- what comes back is decoded
    by the RX listener, not looped in software."""
    t0 = time.time()
    try:
        import audio_loopback as al
        c, audio, _symbols = al.build_audio(profile, text.encode()[:32], fec=fec)
    except Exception as exc:
        return {"ok": False, "error": f"build failed: {exc}",
                "elapsed_s": time.time() - t0}
    try:
        import sounddevice as sd
    except Exception as exc:
        return {"ok": False, "error": f"audio backend missing: {exc}. "
                "Install libportaudio2 + pip sounddevice.",
                "elapsed_s": time.time() - t0}
    try:
        info = sd.query_devices(device_out, "output")
        dev_rate = int(info["default_samplerate"])
        audio_up = al.resample(audio, 12000, dev_rate)
        sd.play(audio_up, samplerate=dev_rate, device=device_out, blocking=True)
    except Exception as exc:
        return {"ok": False, "error": f"playback failed: {exc}",
                "elapsed_s": time.time() - t0}
    return {"ok": True, "seconds": len(audio) / 12000.0,
            "elapsed_s": time.time() - t0}


def rx_capture(seconds: float, device_in=None,
               samplerate_in: int | None = None) -> tuple[bool, np.ndarray, str]:
    """Record `seconds` of audio and return it at 12000 Hz mono for the
    real decoder. Returns (ok, samples_12k, message)."""
    try:
        import sounddevice as sd
        import audio_loopback as al
    except Exception as exc:
        return False, np.zeros(0), f"audio backend missing: {exc}"
    try:
        if samplerate_in is None:
            info = sd.query_devices(device_in, "input")
            samplerate_in = int(info["default_samplerate"])
        rec = sd.rec(int(seconds * samplerate_in), samplerate=samplerate_in,
                     channels=1, device=device_in, blocking=True)
        mono = np.asarray(rec[:, 0], dtype=np.float64)
        return True, al.resample(mono, samplerate_in, 12000), ""
    except Exception as exc:
        return False, np.zeros(0), f"record failed: {exc}"


def decode_audio(samples_12k: np.ndarray, profile: str,
                 fec: str = DEFAULT_FEC) -> dict:
    """Run the real modem decoder over recorded 12000 Hz audio."""
    t0 = time.time()
    try:
        import audio_loopback as al
        report = al.decode_from_recording(np.asarray(samples_12k, dtype=float),
                                          profile, fec, "soft")
        report["elapsed_s"] = time.time() - t0
        return report
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}",
                "elapsed_s": time.time() - t0}


def threshold_table(channel: str = "awgn") -> dict:
    if channel == "fading":
        return dict(THRESHOLDS_FADING)
    if channel == "strong":
        return dict(THRESHOLDS_STRONG)
    return dict(THRESHOLDS)


CHANNELS = ("awgn", "fading")
FECS = (DEFAULT_FEC, STRONG_FEC)
