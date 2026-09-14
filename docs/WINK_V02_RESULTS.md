# WINK v0.2 — waveform bake-off results & adaptive-profile validation

## 1. Root cause of the v0.1 failure

v0.1 (8-FSK, 15 baud, 2.5 Hz tone spacing) collapsed between -5 and -10 dB
in the original benchmark. The cause is a waveform-design bug, not a
receiver or FEC weakness: **for non-coherent M-FSK, adjacent tone
detectors need spacing >= the symbol (baud) rate** to be orthogonal. At
15 baud (66.7 ms/symbol), the minimum safe spacing is 15 Hz. v0.1 used
2.5 Hz — six times too tight — so the tone-energy detectors constantly
stole energy from their neighbours, and no amount of stronger FEC could
fix that at the physical layer.

## 2. The v0.1 bake-off's channel model was also invalid

`waveform_bakeoff/wink_bakeoff.py` scored every candidate at 0.0% BER
down to -18 dB regardless of M/baud/spacing. That's because its noise
model held `sigma` fixed while the correct-tone amplitude scaled with
`sqrt(sps)` (samples per symbol) — so any candidate with more samples per
symbol got a free, un-modelled processing-gain bonus baked into the
"SNR" axis. It could not have discriminated between the four candidates
and shouldn't be trusted as a comparison (this was already flagged in
`WINK_BAKEOFF.md`).

## 3. What v0.2 does differently (all files, one folder)

`wink_modem2.py` is a real sample-domain simulation, common to every
candidate:
- continuous-phase audio synthesis at 12 kHz
- AWGN added once, at the waveform-sample level (one shared SNR
  definition, so no candidate gets accidental extra processing gain)
- non-coherent tone-energy (matched-filter) detection, vectorised
- an **unknown carrier-frequency offset** (+-3 Hz, modelling TX/RX
  oscillator mismatch) and an **unknown symbol start time**, both
  recovered by a coarse search against the sync sequence before data
  demod — the "add CFO + timing error" step called for next in the
  original project summary
- the same K=7 rate-1/2 convolutional code, interleaver, and CRC-gated
  packet framing as v0.1 (`wink_core.py`), unchanged

This measures *whole-packet* success through the real chain, not a raw
per-symbol BER on an idealised channel.

## 4. Results

`run_bakeoff2.py` (40 trials/point, -2 to -20 dB) confirms the diagnosis
cleanly:

| candidate | spacing/baud | outcome |
|---|---|---|
| 8-FSK/15Bd/2.5Hz (v0.1 original) | 0.17x | collapses -8→-14 dB, 0% by -14 dB |
| 4-FSK/10Bd/5Hz | 0.5x | starts degrading at -18 dB, 27.5% at -20 dB |
| 8-FSK/7.5Bd/5Hz | 0.67x | 95% at -20 dB, otherwise solid |
| 4-FSK/7.5Bd/6.67Hz | 0.89x | 100% through -20 dB |
| 8-FSK/5Bd/7.5Hz | 1.5x | 100% through -20 dB |

The pattern is exactly what the orthogonality condition predicts:
spacing/baud >= 1 gives clean performance; below that, degradation
appears earlier and more severely the further below 1x you go.

Extending the SNR sweep further down (`results/bakeoff2_extended.csv`, 60
trials/point) to find real 50%-success thresholds for the two winners:

- **4-FSK/7.5Bd/6.67Hz: ~-22 dB** (100%@-21, 62%@-22, 7%@-23)
- **8-FSK/5Bd/7.5Hz: ~-23.5 dB** (93%@-22, 67%@-23, 8%@-24)

Both candidates carry the same coded throughput (7.5 bit/s after rate-1/2
FEC), but 8-FSK/5Bd/7.5Hz is ~1.5 dB more sensitive at the cost of ~2.25x
more occupied bandwidth (60 Hz vs 26.7 Hz) — the standard MFSK
bandwidth/sensitivity trade.

## 5. Chosen v0.2 profiles (`wink_profiles.py`)

Three profiles, all 8-FSK with spacing == baud (orthogonality-safe by
construction), spanning the WINK-S/N/F design in the project summary:

| profile | baud | spacing | occupied | coded bps | measured 50% threshold |
|---|---|---|---|---|---|
| WINK-S | 5 | 7.5 Hz | 60 Hz | 7.5 | **-23.5 dB** |
| WINK-N | 15 | 15 Hz | 120 Hz | 22.5 | **-18.5 dB** |
| WINK-F | 30 | 30 Hz | 240 Hz | 45 | **-15.5 dB** |

Each ~3x speed step costs roughly 5 dB of sensitivity — consistent with
expected FSK behaviour, and a sane basis for the adaptive-profile
hysteresis logic.

## 6. Adaptive-profile logic, validated end to end

`wink_profiles.recommend_profile()` implements the receiver-driven
adaptation from the project summary: step down immediately if the
current profile has lost its margin, step up one notch at a time only
once the faster profile has margin too (prevents mode-flapping).

`demo_adaptive_qso.py` runs a full simulated QSO — TEXT packets, then a
LINK_REPORT packet carrying measured SNR and the recommended profile —
through the real channel model as SNR rises and falls over 12 exchanges.
Result: correctly stayed on WINK-S while too weak to copy at all
(-25/-24 dB), stepped up to WINK-N once conditions supported it, stepped
back down to WINK-S as the band faded, with no flapping. This is the
first time packet layer + physical layer + adaptive-profile logic have
been exercised together as one system.

## 7. What's still not done (v0.2 does NOT claim these)

- **Audio I/O loopback tooling exists, no on-air validation yet.**
  `audio_loopback.py` plays/records real audio and decodes the capture
  (plus a `--mock` in-process mode that verifies the toolchain), but no
  packet has touched a sound card or radio in testing so far (§15).
- **Fading is modelled, multipath selectively.** Flat Rayleigh/Rician
  fading (`wink_fading.py`) plus a static two-path profile are measured
  (§14); fast selective fading with long delay spreads, Doppler spread
  above a few Hz, and real RF impairments are still unmodelled.
- **No MCU/beacon implementation.** Still a design concept.
- **Mesh relay logic tested, no on-air mesh yet.** Opt-in flood relay
  with TTL + dedup passes 3-node, 5-node line, diamond (exactly-once
  delivery) and broadcast topologies through the real channel
  (`demo_mesh_relay.py`); no routing tables and no real multi-station
  RF test.
- **LLRs now use max-log soft-decision** (per-symbol tone-energy
  margins), the K9 rate-1/2 default, and an opt-in **K9 rate-1/3**
  mode (`simulate_packet(fec="K9_1_3", ...)`) for the highest packet
  success rate at ~1.5x time-on-air. See sections 8 and 12.
- AWGN numbers are idealised-channel results with no real RF
  impairments; fading numbers (§14, §16) are simulated HF, not
  over-the-air measurements. A -23 dB "sensitivity" number here should
  be read as "the waveform + FEC combination is fundamentally sound,"
  not as a claimed on-air decode threshold.

## 8. Error-correction improvements (this round)

Two changes, tested independently and combined, on WINK-S near its
threshold (`results/winkS_fec_comparison.csv`, 60 trials/point):

1. **Soft-decision LLRs.** The receiver previously threw away
   confidence information: every bit got a fixed +-5.0 LLR regardless
   of how clean or noisy that symbol actually was. `wink_modem2.
   soft_llrs_from_energies()` now computes a real max-log LLR per bit
   from the actual tone-energy margins, normalised by each symbol's own
   noise-floor estimate (so a symbol that landed in a noisy instant is
   automatically down-weighted relative to a clean one — the whole
   point of soft-decision decoding).
2. **Stronger convolutional code.** Added K=9 (Voyager-standard,
   dfree=12, generators 753/561 octal) alongside the existing K=7
   (dfree=10). Both are now selectable per-packet via
   `simulate_packet(..., fec=..., llr_mode=...)`.

| FEC | LLR mode | WINK-S 50%-threshold |
|---|---|---|
| K7 | hard (v0.1/v0.2-original) | -23.2 dB |
| K7 | soft | -24.0 dB |
| K9 | hard | -23.6 dB |
| K9 | soft | **-24.3 dB** |

Soft decoding alone accounts for most of the gain (~0.9 dB); the
stronger code adds a further ~0.3 dB on top of that. Re-validating
WINK-N and WINK-F with the winning K9+soft combo
(`results/winkNF_K9soft.csv`) shows the same ~1-1.2 dB improvement holds across
all three profiles:

| profile | old threshold (K7/hard) | new threshold (K9/soft) |
|---|---|---|
| WINK-S | -23.2 dB | **-24.3 dB** |
| WINK-N | -18.5 dB | **-19.5 dB** |
| WINK-F | -15.5 dB | **-16.7 dB** |

K9+soft is now the default (`wink_profiles.DEFAULT_FEC`/
`DEFAULT_LLR_MODE`); the adaptive QSO demo re-run with it decodes text
at -24 dB where the old receiver failed, and now exercises all three
profile transitions (S→N→F→N→S) as the simulated band opens and closes.

A 1-1.2 dB gain is a real, measured improvement, not a dramatic one —
that's expected: soft-decision decoding over hard-decision is a
well-known, bounded gain (textbook range is roughly 2 dB for AWGN
channels; less here since the max-log LLR approximation and per-symbol
noise-floor estimate aren't a fully calibrated likelihood). Larger
further gains would need a structurally different approach (e.g. a
longer/lower-rate code, real diversity combining, or accepting more
latency for repeat transmissions) rather than tuning this same
convolutional/Viterbi scheme further.

## 10. Diversity combining (optional, costs time-on-air)

`simulate_packet_diversity()` sends the same packet over 2 or 3
independent channel draws (independent CFO/timing/noise, as if
retransmitted a few seconds apart) and soft-combines the LLRs before one
Viterbi decode. Measured on WINK-S (K9+soft), 50 trials/point:

| repeats | time-on-air | 50%-threshold | gain over 1x |
|---|---|---|---|
| 1x (baseline) | 1x | -24.2 dB | -- |
| 2x | 2x | -25.6 dB | +1.3 dB |
| 3x | 3x | -26.5 dB | +2.3 dB |

This is real and reproduces the expected diminishing-returns curve
(ideal coherent combining would give 3.0 dB / 4.8 dB for 2x/3x; the
measured gain is smaller because this is non-coherent M-FSK combining
with an independent sync search per repeat, not perfect coherent
integration). It's a genuine tool for a beacon or a "please repeat"
retry, not a free upgrade — 3x the sensitivity gain costs 3x the
channel time, which is a real trade-off for a shared HF band, not a
strictly-better setting. It is **not** wired into the default profile
or the adaptive-QSO demo; `n_repeats` stays operator/protocol choice.

## 11. Where returns diminish from here

Stacking what's been measured so far: v0.1 (broken, collapsed ~-10 dB)
→ v0.2 waveform fix (-23.2 dB) → soft decoding + K9 (-24.3 dB) → 3x
diversity (-26.5 dB). That's the real, honestly-measured trajectory —
about 16 dB recovered from the original bug, plus a further ~2.3 dB from
tuning on top of an already-fixed design. Every remaining lever left
(longer codes, more repeats, tighter sync search) buys well under 1 dB
per unit of added complexity or time-on-air from here; there is no
"perfect" left to extract from this architecture. The next real jump in
capability, if wanted, comes from modelling what's still entirely
unmodelled — multipath/fading — which will very likely *cost* some of
this margin back once it's honestly accounted for, not add to it. That
and real audio I/O remain the two steps that would change what these
numbers actually mean, rather than just moving them a fraction of a dB.

## 12. Rate-1/3 FEC (this round) — the current "really high success rate" lever

`wink_core.py` and `wink_modem2.py` are now rate-generic. A third code,
**K9_1_3** (same K=9 trellis, Voyager rate-1/3 generators 557/663/711
octal), is available via `simulate_packet(fec="K9_1_3", ...)` and is the
`wink_profiles.STRONG_FEC` / ULTRA mode. It costs ~1.5x the coded bits
of K9 rate-1/2 (so ~1.5x time-on-air) for the largest honest sensitivity
gain left in this architecture. Benchmarked on WINK-S, soft LLRs,
40 trials/point (`results/fecv3_WINK-S.csv`, `run_fecv3.py`):

| FEC | code rate | success @ -24 | @ -25 | @ -26 | 50%-threshold (WINK-S) |
|---|---|---|---|---|---|
| K9 (default) | 1/2 | 58% | 8% | 0% | -24.1 dB |
| K9_1_3 | 1/3 | 98% | 78% | 10% | **-25.4 dB** |

That is a measured +1.3 dB for ~1.5x airtime — smaller than the
textbook rate-1/3→1/2 gap precisely because the code gain is real but
saturating, and because the max-log soft front end is not a fully
calibrated likelihood. Rate-1/3 is also measurably better on the
WINK-Beacon at its own threshold: K9 was 1/10 at -32 dB, K9_1_3 was
6/10. Diversity combining (section 10) still stacks on top, so the
"make the packet land at any cost" configs are `fec="K9_1_3"` alone, or
`simulate_packet_diversity(n_repeats=3, fec="K9_1_3")` for roughly
another ~2 dB at 3x airtime.

Adaptive-profile switching is unaffected: `demo_adaptive_qso.py
--ultra` runs the same QSO through K9_1_3, confirmed end-to-end with
all three S→N→F→N→S transitions correct and text decoding reliably at
-25 dB (where the rate-1/2 run drops it). `wink_profiles.
THRESHOLDS_STRONG` carries the per-profile ULTRA estimates (WINK-S
-25.5, WINK-N -20.5, WINK-F -17.8 dB). The wideband scanner and beacon
take the same `fec=` parameter — `make_multistation_recording` now
accepts an `fec` argument so a K9_1_3-recorded trace decodes under
K9_1_3 instead of coming back empty from the symbol-count mismatch.

## 14. Fading reality check (this round)

`wink_fading.py` adds the missing channel: flat Rayleigh/Rician
time-selective fading (Clarke sum-of-sinusoids, Doppler spread
`fd_hz`) plus a static two-path frequency-selective profile. SNR stays
referenced to *average* received power, so deep instantaneous fades
still punch through at high average SNR -- exactly what AWGN-only
thresholds hide. `simulate_packet(..., channel=...)` and
`run_bakeoff2.py --channel/--fd/--k-db/--profile` thread it through;
`recommend_profile(..., channel="fading")` uses the measured fading
table (`THRESHOLDS_FADING`), and `demo_adaptive_qso.py --fade` runs the
whole QSO under Rayleigh fd=1 Hz.

Measured 50%-thresholds, K9+soft, 24-30 trials/point
(`fade_ray_*.csv`, `results/fade_ric_S.csv`, `results/fade_2p_F.csv`):

| profile | AWGN | Rayleigh fd=1 Hz | penalty |
|---|---|---|---|
| WINK-S | -24.3 dB | **-22.0 dB** | 2.3 dB |
| WINK-N | -19.5 dB | **-17.0 dB** | 2.5 dB |
| WINK-F | -16.7 dB | **-14.3 dB** | 2.4 dB |

Sanity anchors: Rician K=10 puts WINK-S at -23.3 dB, between AWGN and
Rayleigh as theory demands. A static two-path (2 ms, 2nd path -3 dB)
barely degrades WINK-F (~-14.5 dB) because the 500 Hz null period is
wider than the 240 Hz band and soft-K9 rides it out -- flat
time-selective fading is the harder case, not delay spread at these
bandwidths.

The `--fade` demo also exposes a real protocol consequence: with no
decode there is no LINK_REPORT, so the receiver cannot step the far end
down -- under fading the link stays up-profile and silent instead of
adapting. Receiver-driven adaptation needs a "missed packet" backoff
(timer-based step-down) before this is robust on-air; that is now
visible rather than assumed.

## 15. Audio loopback (this round)

`audio_loopback.py` synthesises the exact TX audio (16-bit PCM,
-6 dBFS), plays+records it through the system device (`sounddevice`
`playrec`, FFT resample to the device rate and back), and decodes the
capture with the real modem plus a coarse window search (the burst sits
at an unknown absolute position in a loopback recording, beyond the
one-symbol `find_sync` window). Report: decode yes/no, payload,
measured CFO, timing, and an SNR estimate from quiet-lead vs signal
region (an honest proxy, typically within ~3 dB of truth).

`--mock <snr>` runs the same pipeline in-process (no audio hardware):
WINK-S decodes at -12 dB with est SNR -14.8 dB, fails at -24 dB --
consistent with the simulated threshold, so the toolchain is verified.
Real loopback needs a looped-back device (cable or PulseAudio/PipeWire
monitor); on a headless box the script reports "PortAudio not found"
instead of faking it. On-air validation (item: beacon RX) is still open.

## 16. FT8 comparison (this round)

`ft8_sim.py` implements the real WSJT-X FT8 frame for head-to-head
measurement, not an approximation: 79 symbols x 0.16 s = 12.64 s,
8-FSK at 6.25 Hz spacing, Costas [3,1,4,0,6,5,2] sync at 0/36/72,
58 data symbols carrying the actual LDPC(174,91) codeword (77 msg bits
+ CRC-14 poly 0x6757; tables from WSJT-X `lib/ft8`, validated --
random codewords satisfy all 83 checks), min-sum BP decoder, same
unknown-CFO/timing search and same SNR convention as WINK.
Approximations, stated: continuous-phase FSK instead of GFSK, no
a-priori decoding. `run_ft8_compare.py` runs both modes through the
identical harness, 24 trials/point (`results/compare_awgn.csv`,
`results/compare_ray.csv`):

| mode | AWGN 50% | Rayleigh fd=1 50% | fading cost |
|---|---|---|---|
| FT8 (12.64 s, 50 Hz, 6.1 bps) | -23.0 dB | -20.3 dB | 2.7 dB |
| WINK-S (K9, ~33 s, 60 Hz) | -24.1 dB | -21.6 dB | 2.5 dB |
| WINK-N (K9) | -19.3 dB | -17.3 dB | 2.0 dB |
| WINK-F (K9) | -16.4 dB | -14.2 dB | 2.2 dB |

Reading it honestly: WINK-S is ~1.1-1.3 dB more sensitive than FT8 in
both channels, at ~2.6x the time-on-air per message bit and a similar
bandwidth. FT8's advantages are structural, not sensitivity: fixed
77-bit messages with a 15-second slot discipline the whole world
already uses, and 12.6 s per exchange vs ~33 s. WINK's advantages:
arbitrary payloads up to 32 bytes, adaptive S/N/F rates, no slot
clock needed. The fading cost is uniform (~2-2.7 dB) across all four --
nothing in either design is specially fragile or specially robust to
flat fading; both would want diversity/repeats on a disturbed path.

## 17. Adaptation margin (this round)

`recommend_profile()`'s `margin_db` was 3.0 dB by habit, not measurement.
`run_margin_sweep.py` runs an 8-step rising/falling SNR track under
Rayleigh fd=1 Hz with +-1.5 dB measurement noise (the receiver never
estimates SNR cleanly, and stays silent on a miss), 48 trials/margin:

| margin | delivered | switches |
|---|---|---|
| 1 dB | 62.5% | 12 |
| 2 dB | 68.8% | 5 |
| 3 dB | 66.7% | 7 |
| 4 dB | 72.9% | 4 |
| 5 dB | 70.8% | 2 |

Small margins don't just flap (12 switches at 1 dB) -- they step up
into profiles that then fail, losing messages. The failure mode is
over-eagerness, not oscillation. Default is now **4.0 dB**: best
delivery with near-minimal switching on this track. The effect is
modest (+-3% delivery), so this is a tuned default, not a breakthrough.

## 18. Suggested next step

Per the project's own priority order: (4) multipath/fading model, then
(5) real audio I/O loopback, then (6) calibrated comparison against a
known mode (e.g. FT8) under the same conditions.
