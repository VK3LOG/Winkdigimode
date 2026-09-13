#!/usr/bin/env python3
"""WINK v0.2 — adaptive-profile QSO demo.

Runs a simulated exchange between two stations over the real channel
model (AWGN + unknown CFO + unknown timing) as the link SNR changes,
with the receiver sending LINK_REPORT packets and the transmitter
following wink_profiles.recommend_profile() to switch between
WINK-S / WINK-N / WINK-F. This exercises packet framing, FEC, the
physical layer, and the adaptive-profile logic together end to end,
which is the "does WINK actually work as a system" question -- not
just "does one waveform decode".
"""
import struct
import sys
import numpy as np
from wink_core import Packet, callsign_id
from wink_modem2 import simulate_packet
from wink_profiles import PROFILES, THRESHOLDS, ORDER, recommend_profile, DEFAULT_FEC, DEFAULT_LLR_MODE, STRONG_FEC

TEXT = 1
LINK_REPORT = 3

VK3ABC = callsign_id("VK3ABC")
VK3XYZ = callsign_id("VK3XYZ")

# Default build uses K9 rate-1/2. Invoke with --ultra on the command line to
# run the same QSO through K9_1_3 (rate-1/3): higher packet success rate at
# ~1.5x time-on-air per packet, plus all three S/N/F transitions still work.
# --fade switches the transmit channel to flat Rayleigh (fd = 1 Hz) AND the
# adaptive thresholds to THRESHOLDS_FADING, so the demo reflects what the
# mode actually promises on an HF channel, not just under AWGN.
USE_ULTRA = "--ultra" in sys.argv[1:]
USE_FADE = "--fade" in sys.argv[1:]
FEC = STRONG_FEC if USE_ULTRA else DEFAULT_FEC
CHANNEL = "fading" if USE_FADE else "awgn"
if USE_FADE:
    from wink_fading import RAYLEIGH_1HZ as TX_CHANNEL
else:
    from wink_fading import AWGN as TX_CHANNEL


def send(profile_name, snr_db, payload, kind, source, dest, seq, seed):
    c = PROFILES[profile_name]
    pkt = Packet(kind, source, dest, seq, payload)
    rng = np.random.default_rng(seed)
    front_pad = int(rng.integers(0, c.sps))
    cfo = float(rng.uniform(-3, 3))
    return simulate_packet(c, pkt, snr_db, rng, cfo, front_pad,
                            fec=FEC, llr_mode=DEFAULT_LLR_MODE, channel=TX_CHANNEL)


def link_report_payload(snr_db, recommended):
    return struct.pack(">hB", int(round(snr_db * 10)), ORDER.index(recommended))


def parse_link_report(payload):
    snr_x10, prof_idx = struct.unpack(">hB", payload[:3])
    return snr_x10 / 10.0, ORDER[prof_idx]


def run():
    # A simulated fading/improving band opening over the course of a QSO.
    snr_timeline = [-25, -24, -22, -19, -16, -14, -13, -14, -17, -21, -25, -27]
    texts = [f"msg {i} from VK3ABC".encode() for i in range(len(snr_timeline))]

    profile = "WINK-S"  # start conservative
    seq = 0
    print(f"FEC: {FEC}  ({'K9 rate-1/2' if not USE_ULTRA else 'K9 rate-1/3 ultra'} + soft LLRs)  "
          f"channel thresholds: {CHANNEL}" + ("  fade TX: Rayleigh fd=1 Hz" if USE_FADE else ""))
    print(f"{'step':>4} {'snr':>6} {'tx profile':>11} {'TEXT ok':>8} {'LINK_REPORT ok':>15} {'next profile':>13}")
    for i, (snr, text) in enumerate(zip(snr_timeline, texts)):
        seq += 1
        rx_text = send(profile, snr, text, TEXT, VK3ABC, VK3XYZ, seq, seed=1000 + i)
        text_ok = rx_text is not None and rx_text.payload == text

        # Receiver only has a usable SNR estimate if it actually decoded
        # something; if not, it stays silent this round rather than
        # guessing (matches "receiver-driven adaptation").
        next_profile = profile
        report_ok = False
        if text_ok:
            recommended = recommend_profile(snr, profile, channel=CHANNEL)
            report_payload = link_report_payload(snr, recommended)
            seq += 1
            rx_report = send(profile, snr, report_payload, LINK_REPORT,
                              VK3XYZ, VK3ABC, seq, seed=2000 + i)
            report_ok = rx_report is not None and rx_report.payload == report_payload
            if report_ok:
                _, next_profile = parse_link_report(rx_report.payload)

        print(f"{i:4d} {snr:6.1f} {profile:>11} {str(text_ok):>8} "
              f"{str(report_ok):>15} {next_profile:>13}")
        profile = next_profile


if __name__ == "__main__":
    run()
