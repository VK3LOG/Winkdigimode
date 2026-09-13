#!/usr/bin/env python3
"""Generates reference test vectors from wink_beacon.py (the validated
Python reference) for checking wink_beacon.h's C++ port against.

Run this from the main folder where wink_beacon.py lives, e.g.:
    python3 beacon/gen_test_vectors.py

To check the C++ port: build a throwaway sketch/test harness that calls
winkbeacon::encode_beacon_symbols() with the same (callsign, locator,
power) triples below and diff the printed raw bytes / symbol arrays
against this file's output. This has NOT been done yet -- there is no
automated cross-check, this script only produces the vectors to check
against.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from wink_beacon import encode_beacon, WINK_BEACON
from wink_core import conv_encode, interleave, FEC_CONFIGS, bytes_to_bits
from wink_modem2 import bits_to_symbols_gray

CASES = [
    ("VK3LOG", "QF22", 27),
    ("K1ABC", "FN42", 37),
    ("W1AW", "FN31", 23),
]

if __name__ == "__main__":
    fp = FEC_CONFIGS["K9"]
    for callsign, locator, power in CASES:
        raw = encode_beacon(callsign, locator, power)
        bits = bytes_to_bits(raw)
        coded = conv_encode(bits, K=fp["K"], G0=fp["G0"], G1=fp["G1"])
        shuffled = interleave(coded)
        symbols = bits_to_symbols_gray(shuffled, WINK_BEACON.k)
        print(f"# {callsign} {locator} {power}dBm")
        print(f"raw_hex    = {raw.hex()}")
        print(f"coded_len  = {len(coded)}")
        print(f"symbols    = {symbols}")
        print()
