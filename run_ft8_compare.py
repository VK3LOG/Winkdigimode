#!/usr/bin/env python3
"""WINK vs FT8 head-to-head: same channel models, same SNR convention.

Both modes run through the identical impairment harness (unknown CFO
+-3 Hz, unknown start within one symbol, AWGN at sample level, optional
flat Rayleigh fading fd=1 Hz via wink_fading) and the identical success
criterion (payload/CRC must verify). SNR is mean power over the burst in
both cases, so the dB numbers are directly comparable -- the remaining
differences are the modes themselves (code strength, symbol time,
bandwidth, time-on-air).

Throughput context (see ft8_sim.frame_info):
  FT8:   77 msg bits / 12.64 s = 6.1 bps, 50 Hz BW, fixed 15 s slot
  WINK-S: 15-byte test payload, ~33 s/packet, 60 Hz BW (slowest profile)

Usage:
  python3 run_ft8_compare.py --channel awgn --out compare_awgn.csv
  python3 run_ft8_compare.py --channel rayleigh --out compare_ray.csv
"""
import argparse, csv, sys, time
import numpy as np

from wink_modem2 import simulate_packet
from wink_core import Packet, callsign_id
from wink_profiles import PROFILES
from wink_fading import FadingChannel
import ft8_sim

PAYLOAD = b"hello from WINK"
TRIALS = 24

SWEEPS_AWGN = {
    "FT8":    [-25, -24, -23, -22, -21],
    "WINK-S": [-26, -25, -24, -23],
    "WINK-N": [-21, -20, -19, -18],
    "WINK-F": [-18, -17, -16, -15],
}
SWEEPS_RAY = {
    "FT8":    [-23, -22, -21, -20, -19],
    "WINK-S": [-24, -23, -22, -21],
    "WINK-N": [-19, -18, -17, -16],
    "WINK-F": [-16, -15, -14, -13],
}


def run_mode(mode, snr, rng, ch):
    if mode == "FT8":
        m77 = rng.integers(0, 2, size=77)
        out = ft8_sim.simulate_ft8(
            m77, snr, rng, float(rng.uniform(-3, 3)),
            int(rng.integers(0, ft8_sim.SPS)), channel=ch)
        return out is not None and (out[:77] == m77).all()
    c = PROFILES[mode]
    pkt = Packet(1, callsign_id("VK3TEST"), callsign_id("VK5TEST"), 1, PAYLOAD)
    rx = simulate_packet(c, pkt, snr, rng, float(rng.uniform(-3, 3)),
                         int(rng.integers(0, c.sps)),
                         fec="K9", llr_mode="soft", channel=ch)
    return rx is not None and rx.payload == pkt.payload


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--channel", choices=("awgn", "rayleigh"), default="awgn")
    p.add_argument("--trials", type=int, default=TRIALS)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    ch = FadingChannel(kind="rayleigh", fd_hz=1.0) if a.channel == "rayleigh" \
        else FadingChannel(kind="awgn")
    sweeps = SWEEPS_RAY if a.channel == "rayleigh" else SWEEPS_AWGN
    rows = []
    t0 = time.time()
    for mode, snrs in sweeps.items():
        for snr in snrs:
            good = 0
            for t in range(a.trials):
                rng = np.random.default_rng(hash((mode, snr, t, a.channel)) & 0xFFFFFFFF)
                try:
                    good += int(run_mode(mode, float(snr), rng, ch))
                except Exception:
                    pass
            rows.append([mode, snr, good, a.trials, good / a.trials])
            print(f"{mode:6} {snr:6.1f} dB  {good:3d}/{a.trials}  "
                  f"({100*good/a.trials:5.1f}%)  elapsed={time.time()-t0:5.0f}s",
                  file=sys.stderr, flush=True)
    out = a.out or f"compare_{a.channel}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mode", "snr_db", "successful", "trials", "success_rate"])
        w.writerows(rows)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
