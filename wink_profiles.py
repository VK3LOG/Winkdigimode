"""WINK v0.2 — validated waveform profiles.

Each profile's tone spacing satisfies the non-coherent M-FSK orthogonality
condition (spacing >= baud rate). This is the fix for the v0.1 failure:
v0.1 used 2.5 Hz spacing at 15 baud (spacing = baud/6), so adjacent tone
detectors interfered heavily and the mode collapsed well before -10 dB.

threshold_snr_db below is the empirical 50%-packet-success point measured
by run_bakeoff2.py + the extended/NF sweeps, using the real
sample-domain channel model (AWGN + unknown CFO +-3 Hz + unknown symbol
timing, both recovered by search) -- not the idealised v0.1 bake-off
model. Treat these as simulation results, not an over-the-air sensitivity
claim; see WINK_V02_RESULTS.md for caveats (no multipath, no real audio
I/O yet).

DEFAULT_FEC / DEFAULT_LLR_MODE: as of the winkS_fec_comparison.csv /
winkNF_K9soft.csv sweep, K9 (Voyager-standard convolutional code) +
max-log soft-decision LLRs (see wink_modem2.soft_llrs_from_energies)
beat the v0.1-carried-over K7 + fixed-magnitude hard-decision LLRs by
~1-1.2 dB across all three profiles, so they're now the default. K7/hard
is still selectable via simulate_packet(..., fec="K7", llr_mode="hard")
for comparison.
"""
from dataclasses import dataclass
from wink_modem2 import Candidate

WINK_S = Candidate("WINK-S", M=8, baud=5, spacing=7.5)
WINK_N = Candidate("WINK-N", M=8, baud=15, spacing=15)
WINK_F = Candidate("WINK-F", M=8, baud=30, spacing=30)

DEFAULT_FEC = "K9"
DEFAULT_LLR_MODE = "soft"

# Max-reliability FEC used by ULTRA mode. Same K=9, rate 1/3
# (generators 557/663/711 octal) -- ~1.5-2 dB better than K9 rate 1/2 at
# 1.5x time-on-air. Selectable per-call via simulate_packet(fec="K9_1_3").
STRONG_FEC = "K9_1_3"

# Measured 50%-success SNR thresholds (dB) with the default K9+soft
# receiver, from winkS_fec_comparison.csv / winkNF_K9soft.csv.
# ~100% success sits roughly 1-2 dB above these.
THRESHOLDS = {
    "WINK-S": -24.3,
    "WINK-N": -19.5,
    "WINK-F": -16.7,
}

# Estimated 50%-success thresholds for the same profiles with
# K9_1_3+soft (rate-1/3), anchored on the measured WINK-S delta from
# run_fecv3.py (K9 -24.1 dB -> K9_1_3 -25.4 dB, +1.3 dB) and assuming
# the same FEC-internal gain holds for WINK-N/F as the K9 validation did.
# Use STRONG_FEC when a beacon or "please repeat" needs the highest
# packet success rate.
THRESHOLDS_STRONG = {
    "WINK-S": -25.5,
    "WINK-N": -20.5,
    "WINK-F": -17.8,
}

# For reference: thresholds with the original v0.1-carried-over
# K7 + hard-decision receiver (bakeoff2_extended.csv / bakeoff2_NF_profiles.csv).
THRESHOLDS_K7_HARD = {
    "WINK-S": -23.2,
    "WINK-N": -18.5,
    "WINK-F": -15.5,
}

# Honest HF reality check (K9 + soft LLRs, measured by run_bakeoff2.py):
# 50%-success thresholds under flat Rayleigh fading, fd = 1 Hz, SNR
# referenced to *average* received power, 24-30 trials/point
# (fade_ray_S/N/F.csv). A deep fade can punch through even at high
# average SNR, which is precisely what AWGN-only thresholds hide.
# WINK-S degrades -24.3 -> -22.0 dB, WINK-N -19.5 -> -17.0 dB,
# WINK-F -16.7 -> -14.3 dB. Rician K=10 confirms the shape: WINK-S sits
# at -23.3 dB, between AWGN and Rayleigh (fade_ric_S.csv).
# A stationary two-path (2 ms delay, 2nd path -3 dB) barely degrades
# WINK-F (-16.7 -> ~-14.5 dB) because the 500 Hz null period is wider
# than the 240 Hz band and soft-K9 rides it out; flat time-selective
# fading is the harder case (fade_2p_F.csv).
#
# recommend_profile(..., channel="fading") uses these instead of the
# AWGN table, so adaptive links can be tuned for the deployed channel.
THRESHOLDS_FADING = {
    "WINK-S": -22.0,
    "WINK-N": -17.0,
    "WINK-F": -14.3,
}

PROFILES = {"WINK-S": WINK_S, "WINK-N": WINK_N, "WINK-F": WINK_F}
# Ordered slowest/most-robust -> fastest/least-robust.
ORDER = ["WINK-S", "WINK-N", "WINK-F"]

THRESHOLD_TABLES = {
    "awgn": THRESHOLDS,
    "fading": THRESHOLDS_FADING,
    "strong": THRESHOLDS_STRONG,
}


def recommend_profile(measured_snr_db: float, current: str, margin_db: float = 4.0,
                      channel: str = "awgn") -> str:
    """Receiver-side profile recommendation for the far end's next
    transmission, per WINK_PROJECT_SUMMARY.md section 3 (adaptive links).

    Picks the fastest profile whose threshold still has margin_db of
    headroom below the measured SNR, then applies hysteresis: only
    recommends stepping down (to a more robust profile) if the current
    profile is actually failing to meet its own margin, and only
    recommends stepping up (faster) one step at a time, so a single noisy
    measurement can't cause mode-flapping.

    channel: "awgn" (default, THRESHOLDS), "fading" (THRESHOLDS_FADING,
    measured Rayleigh flat-fading 50% points), or "strong"
    (THRESHOLDS_STRONG for the K9_1_3 ULTRA FEC).
    """
    thresholds = THRESHOLD_TABLES[channel]
    idx = ORDER.index(current)

    # Step down if current profile no longer has margin.
    if measured_snr_db < thresholds[current] + margin_db:
        while idx > 0 and measured_snr_db < thresholds[ORDER[idx]] + margin_db:
            idx -= 1
        return ORDER[idx]

    # Step up (one notch) if the next-faster profile already has margin.
    if idx < len(ORDER) - 1:
        faster = ORDER[idx + 1]
        if measured_snr_db >= thresholds[faster] + margin_db:
            return faster

    return current
