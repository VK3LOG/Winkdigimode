#!/usr/bin/env python3
"""Sweep recommend_profile's margin_db through a noisy, time-varying
SNR track under Rayleigh fading: does margin 3.0 flap, and would a
smaller/larger margin deliver more text with fewer mode switches?

Track: 8 steps rising/falling through all three profiles' fading
thresholds, with +-1.5 dB measurement noise (the receiver estimates
SNR from real decodes, never cleanly). One TEXT exchange per step;
success = CRC-verified decode. Counts delivered messages (opportunity)
and profile switches (flapping). 3 seeds x margins 1..5.

Usage: python3 run_margin_sweep.py   (takes ~5 min, writes margin_sweep.csv)
"""
import csv, sys, time
import numpy as np
from wink_core import Packet, callsign_id
from wink_modem2 import simulate_packet
from wink_profiles import PROFILES, ORDER, recommend_profile, DEFAULT_FEC, DEFAULT_LLR_MODE
from wink_fading import RAYLEIGH_1HZ

TRACK = [-24, -22, -20, -17, -14, -15, -19, -23]
MARGINS = [1.0, 2.0, 3.0, 4.0, 5.0]
SEEDS = (11, 22, 33, 44, 55, 66)


def run_track(margin, seed):
    rng = np.random.default_rng(hash(("margin", margin, seed)) & 0xFFFFFFFF)
    profile = "WINK-S"
    delivered, switches = 0, 0
    for i, true_snr in enumerate(TRACK):
        c = PROFILES[profile]
        pkt = Packet(1, callsign_id("VK3ABC"), callsign_id("VK3XYZ"), 1,
                     f"m{i}".encode())
        rx = simulate_packet(c, pkt, true_snr, rng, float(rng.uniform(-3, 3)),
                             int(rng.integers(0, c.sps)),
                             fec=DEFAULT_FEC, llr_mode=DEFAULT_LLR_MODE,
                             channel=RAYLEIGH_1HZ)
        ok = rx is not None and rx.payload == pkt.payload
        delivered += int(ok)
        # Receiver's SNR estimate is noisy; no estimate at all on a miss
        # (receiver-driven adaptation stays silent -- honest limitation).
        if ok:
            est = true_snr + float(rng.normal(0, 1.5))
            nxt = recommend_profile(est, profile, margin_db=margin, channel="fading")
        else:
            nxt = profile
        switches += int(nxt != profile)
        profile = nxt
    return delivered, switches


def main():
    rows = []
    t0 = time.time()
    for margin in MARGINS:
        d_tot, s_tot = 0, 0
        for seed in SEEDS:
            d, s = run_track(margin, seed)
            d_tot += d
            s_tot += s
        n = len(TRACK) * len(SEEDS)
        rows.append([margin, d_tot, n, d_tot / n, s_tot])
        print(f"margin {margin:.0f} dB: delivered {d_tot:2d}/{n} "
              f"({100*d_tot/n:4.1f}%)  switches {s_tot:2d}  "
              f"elapsed={time.time()-t0:4.0f}s", file=sys.stderr, flush=True)
    with open("margin_sweep.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["margin_db", "delivered", "trials", "delivery_rate", "switches"])
        w.writerows(rows)
    print("wrote margin_sweep.csv")


if __name__ == "__main__":
    main()
