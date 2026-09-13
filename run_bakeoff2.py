#!/usr/bin/env python3
"""WINK v0.2 waveform bake-off — realistic packet-success benchmark.

Differences from the v0.1 bake-off (waveform_bakeoff/wink_bakeoff.py):
- real sample-domain AWGN (one SNR definition shared fairly across
  candidates, instead of accidentally baking in extra processing gain
  for whichever candidate has more samples per symbol)
- unknown per-packet carrier frequency offset, recovered by search
- unknown per-packet start time, recovered by search
- measures whole-packet success through the real FEC/interleaver/CRC
  chain, not just a raw symbol/bit error rate on an idealised channel

Channel model is selectable now:
  python3 run_bakeoff2.py --channel rayleigh --fd 1.0
  python3 run_bakeoff2.py --channel rician --k-db 10
  python3 run_bakeoff2.py --channel two_path --delay-ms 2
  (default: awgn -- identical to the original sweep)
SNR is referenced to average received power for fading channels.
"""
import argparse, csv, sys, time
import numpy as np
from wink_modem2 import CANDIDATES, simulate_packet
from wink_core import Packet, callsign_id
from wink_fading import FadingChannel, dry_info

PROFILES = ("8FSK-5Bd-7.5Hz", "8FSK-15Bd-15Hz", "8FSK-30Bd-30Hz")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--channel", choices=("awgn", "rayleigh", "rician", "two_path"),
                   default="awgn")
    p.add_argument("--fd", type=float, default=1.0, help="Doppler spread Hz (flat fading)")
    p.add_argument("--k-db", type=float, default=None, help="Rician K factor dB (rician)")
    p.add_argument("--delay-ms", type=float, default=2.0, help="second-path delay ms (two_path)")
    p.add_argument("--second-path-db", type=float, default=-3.0,
                   help="relative level of delayed replica dB")
    p.add_argument("--snrs", nargs="*", type=float, default=None,
                   help="SNR points (dB); default = the 10-point sweep")
    p.add_argument("--trials", type=int, default=40)
    p.add_argument("--fec", choices=("K7", "K9", "K9_1_3"), default="K9")
    p.add_argument("--llr", choices=("soft", "hard"), default="soft")
    p.add_argument("--candidates", nargs="*", default=None,
                   help="restrict to named CANDIDATES (default: all)")
    p.add_argument("--profile", nargs="*", choices=("S", "N", "F"), default=None,
                   help="use the shipped WINK-S/N/F Candidates from wink_profiles instead")
    p.add_argument("--out", default=None, help="output CSV path")
    a = p.parse_args(argv)
    a.channel_obj = FadingChannel(
        kind=a.channel,
        fd_hz=a.fd,
        k_db=float(a.k_db) if a.k_db is not None else float("-inf"),
        delay_s=a.delay_ms / 1000.0,
        second_path_gain_db=a.second_path_db,
    )
    return a


def run(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    snrs = args.snrs or [-2, -4, -6, -8, -10, -12, -14, -16, -18, -20]
    if args.profile is not None:
        from wink_profiles import PROFILES as WINK_PROFILES
        cands = [WINK_PROFILES[f"WINK-{name}"] for name in args.profile]
    else:
        cands = [c for c in CANDIDATES if args.candidates is None or c.name in args.candidates]
    pkt = Packet(1, callsign_id("VK3TEST"), callsign_id("VK5TEST"), 1, b"hello from WINK")
    ch = args.channel_obj
    rows = []
    t_start = time.time()
    print(f"channel: {dry_info(ch)}  fec={args.fec}  llr={args.llr}", file=sys.stderr)
    for c in cands:
        for snr in snrs:
            good = 0
            for t in range(args.trials):
                rng = np.random.default_rng(hash((c.name, snr, t, dry_info(ch), args.fec)) & 0xFFFFFFFF)
                front_pad = int(rng.integers(0, c.sps))
                true_cfo = float(rng.uniform(-3, 3))
                try:
                    rx = simulate_packet(c, pkt, snr, rng, true_cfo, front_pad,
                                         fec=args.fec, llr_mode=args.llr, channel=ch)
                except Exception:
                    rx = None
                good += int(rx is not None and rx.payload == pkt.payload)
            rows.append([c.name, snr, good, args.trials, good / args.trials,
                         c.coded_bps, c.occupied_hz])
            print(f"{c.name:32} {snr:5.1f} dB  {good:3d}/{args.trials}  "
                  f"({100*good/args.trials:5.1f}%)  elapsed={time.time()-t_start:5.0f}s",
                  file=sys.stderr)

    out = args.out or f"bakeoff2_{args.channel}_{args.fec}-{args.llr}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["candidate", "snr_db", "successful", "trials",
                    "success_rate", "coded_bps", "occupied_hz", "channel"])
        w.writerows(r + [dry_info(ch)] for r in rows)
    print(f"wrote {out} ({dry_info(ch)})")


if __name__ == "__main__":
    run()
