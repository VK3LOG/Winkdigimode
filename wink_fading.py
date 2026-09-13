"""WINK v0.2 — multipath / fading channel model.

Replaces the AWGN-only assumption with two honest HF channel shapes:

* flat (Rayleigh/Rician) time-selective fading via a Clarke/Jakes
  sum-of-sinusoids draw (Doppler spread ``fd_hz``). Rician ``k_db`` is
  the LOS-to-diffuse ratio; ``k_db = -inf`` is pure Rayleigh. The
  envelope ``|h[n]|`` multiplies the passband samples, which is the
  correct impairment for a non-coherent energy-detector M-FSK receiver
  (phase is irrelevant, amplitude dips are what lose symbols). The gain
  is normalised so E[|h|^2] = 1, meaning the SNR is measured at the
  ***average*** received power -- the standard convention for plotting
  Rayleigh/Rician curves. Deep instantaneous fades still punch through
  even at high average SNR: that is exactly the reality an AWGN-only
  threshold hides.

* two_path: a direct path plus one delayed replica (fixed relative
  delay/gain). This creates the frequency-selective nulls HF shows at
  wider bandwidths -- WINK-F (240 Hz) is more exposed to this than
  WINK-S (60 Hz). Static by design so the nulls land on *specific*
  tones: it is a worst-case-shaped (deterministic) spot check, not an
  average.

Both are driven through ``FadingChannel`` and applied by
``wink_modem2.simulate_packet(..., channel=ch)`` so everything else in
the stack (sync search, CFO recovery, soft LLRs, FEC, diversity) is
reused unchanged.
"""

from __future__ import annotations
from dataclasses import dataclass
import numpy as np

FS = 12000  # same as wink_modem2.FS


@dataclass(frozen=True)
class FadingChannel:
    """Channel impairment spec passed to simulate_packet().

    kind: "awgn" (no fading), "rician"/"rayleigh" (flat time-selective),
          "two_path" (frequency-selective delay profile).
    fd_hz: Doppler spread for flat fading (typical HF 0.5-5 Hz).
    k_db: Rician K factor (dB). ceil(None) or -inf => pure Rayleigh.
    delay_s: second-path delay for two_path.
    second_path_gain_db: relative level of the delayed replica.
    """

    kind: str = "awgn"
    fd_hz: float = 1.0
    k_db: float = float("-inf")
    delay_s: float = 0.002
    second_path_gain_db: float = -3.0
    num_sinusoids: int = 8

    def __post_init__(self) -> None:
        if self.kind not in ("awgn", "rayleigh", "rician", "two_path"):
            raise ValueError(f"unknown channel kind: {self.kind!r}")
        if self.kind in ("rician",) and self.k_db == float("-inf"):
            object.__setattr__(self, "kind", "rayleigh")
        if self.fd_hz < 0 or self.delay_s < 0:
            raise ValueError("fd_hz and delay_s must be >= 0")


AWGN = FadingChannel(kind="awgn")
RAYLEIGH_1HZ = FadingChannel(kind="rayleigh", fd_hz=1.0)
RAYLEIGH_2HZ = FadingChannel(kind="rayleigh", fd_hz=2.0)
RICIAN_K10_1HZ = FadingChannel(kind="rician", k_db=10.0, fd_hz=1.0)
TWO_PATH_2MS = FadingChannel(kind="two_path", delay_s=0.002, second_path_gain_db=-3.0)


def _clarke_complex(n_samples: int, fd_hz: float, rng: np.random.Generator,
                    n: int = 8) -> np.ndarray:
    """Normalised Clarke/Jakes complex flat-fading process h[n],
    E[|h|^2] = 1, phases uniformly random (isotropic scattering)."""
    if fd_hz <= 0 or n_samples <= 1:
        return np.ones(n_samples, dtype=complex)
    n = max(int(n), 2)
    t = np.arange(n_samples)
    # Isotropic scattering: N incident waves, Doppler-shifted by f_d cos(alpha_i).
    alpha = rng.uniform(0, 2 * np.pi, size=n)
    phi = rng.uniform(0, 2 * np.pi, size=n)
    w_d = 2 * np.pi * fd_hz * np.cos(alpha)  # per-wave angular Doppler
    real_part = np.zeros(n_samples)
    imag_part = np.zeros(n_samples)
    for i in range(n):
        arg = w_d[i] * t / FS + phi[i]
        real_part += np.cos(arg)
        imag_part += np.sin(arg)
    return (real_part + 1j * imag_part) / np.sqrt(n)


def apply_channel(x: np.ndarray, rng: np.random.Generator,
                  ch: FadingChannel = AWGN) -> np.ndarray:
    """Apply one channel realization to the passband burst ``x``.

    Returns a same-length real float array. AWGN is added *after* this
    by the caller (as usual), so snr_db stays referenced to the average
    received power for fading channels.
    """
    if ch.kind == "awgn":
        return np.asarray(x, dtype=float)
    if ch.kind in ("rayleigh", "rician"):
        h = _clarke_complex(len(x), ch.fd_hz, rng, ch.num_sinusoids)
        if ch.kind == "rician":
            k = 10.0 ** (max(ch.k_db, 0.0) / 10.0)
            los = np.sqrt(k / (k + 1.0))
            diffuse = np.sqrt(1.0 / (k + 1.0))
            h = los * np.exp(1j * rng.uniform(0, 2 * np.pi)) + diffuse * h
        return (np.asarray(x, dtype=float) * np.abs(h))
    if ch.kind == "two_path":
        delay_samps = max(1, int(round(ch.delay_s * FS)))
        a = 10.0 ** (ch.second_path_gain_db / 20.0)
        xp = np.asarray(x, dtype=float)
        out = np.zeros_like(xp)
        # direct path + delayed replica; early/late edges hold over
        out[delay_samps:] += xp[:-delay_samps]
        out[: delay_samps] += xp[:delay_samps]
        out += a * xp
        return out
    raise ValueError(f"unhandled channel kind {ch.kind!r}")


def dry_info(ch: FadingChannel) -> str:
    if ch.kind == "awgn":
        return "awgn"
    if ch.kind == "rayleigh":
        return f"rayleigh flat, fd={ch.fd_hz} Hz"
    if ch.kind == "rician":
        return f"rician flat, K={ch.k_db} dB, fd={ch.fd_hz} Hz"
    if ch.kind == "two_path":
        return f"two-path, delay={ch.delay_s*1e3:.0f} ms, 2nd path {ch.second_path_gain_db} dB"
    return ch.kind