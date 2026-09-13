#!/usr/bin/env python3
"""WINK v0.3-ish FEC comparison -- K9 (rate 1/2) vs K9_1_3 (rate 1/3).

Both use the same soft-decision max-log LLR front end. Rate 1/3 costs
1.5x the coded bits (hence ~1.5x time-on-air) for roughly 1.5-2 dB more
coding gain. This script re-measures the delta on a chosen profile so
the adaptive-profile thresholds stay grounded in data instead of guesswork.

Run from the main folder:  python3 run_fecv3.py [profile] [snrs...]
Default: WINK-S at -24 -25 -26 -27 (40 trials/point, ~4 min).
Writes fecv3_<profile>.csv.
"""
import csv, sys, time
import numpy as np
from wink_modem2 import simulate_packet
from wink_core import Packet, callsign_id
from wink_profiles import PROFILES

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "WINK-S"
SNRS = [int(x) for x in sys.argv[2:]] or [-24, -25, -26, -27]
TRIALS = 40
PAYLOAD = b"hello from WINK"
FECS = ("K9", "K9_1_3")


def run():
    c = PROFILES[PROFILE]
    pkt = Packet(1, callsign_id("VK3TEST"), callsign_id("VK5TEST"), 1, PAYLOAD)
    rows = []
    t0 = time.time()
    for fec in FECS:
        for snr in SNRS:
            good = 0
            for t in range(TRIALS):
                rng = np.random.default_rng(hash((PROFILE, fec, snr, t)) & 0xFFFFFFFF)
                rx = simulate_packet(c, pkt, snr, rng,
                                      float(rng.uniform(-3, 3)),
                                      int(rng.integers(0, c.sps)),
                                      fec=fec, llr_mode="soft")
                good += int(rx is not None and rx.payload == pkt.payload)
            rows.append([fec, snr, good, TRIALS, good / TRIALS])
            print(f"{PROFILE:6} {fec:8} {snr:5.1f} dB  {good:3d}/{TRIALS} "
                  f"({100*good/TRIALS:5.1f}%)  elapsed={time.time()-t0:4.0f}s",
                  file=sys.stderr)
    out = f"fecv3_{PROFILE}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fec", "snr_db", "successful", "trials", "success_rate"])
        w.writerows(rows)
    print(f"wrote {out}")


if __name__ == "__main__":
    run()