# WINK-Beacon / WSPR dual-mode ESP32 transmitter firmware

**Status: unvalidated reference skeleton.** This has not been run on real
hardware — I (Claude) don't have a bench to test against. Treat it as a
correctly-structured starting point, not something to key a PA and
transmit on air with unmodified. See "Before you transmit" at the bottom.

## Hardware assumptions

- **ESP32-S3 or ESP32-C3** (native USB) — required for genuine
  single-cable USB-C power + flash. A classic ESP32 (no native USB) needs
  a separate USB-UART bridge chip and won't meet that requirement.
- Si5351 clock generator (I2C) as the RF synth — the near-universal choice
  for this class of QRP beacon and what JTEncode/Etherkit-Si5351 target.
- External PA + low-pass filter bank to reach 5 W and clean up the Si5351's
  square-wave output before the BNC — **not designed here**. That's an RF
  hardware design task (PA device selection, LPF per band, harmonic
  filtering to meet spurious-emission limits) that needs to be done on the
  bench with a spectrum analyzer, not guessed in firmware. This code only
  drives a PA-enable GPIO and assumes the analog chain exists.
- One PTT/PA-enable GPIO, optionally a band-select relay bank (stubbed).

## Dependencies (Arduino/PlatformIO)

- [Etherkit Si5351](https://github.com/etherkit/Si5351Arduino) — Si5351 driver
- [JTEncode](https://github.com/etherkit/JTEncode) — standard, verified WSPR/JT65/FT8
  symbol encoder. **Use this for real WSPR, don't hand-roll it** — the
  exact bit-packing and 162-symbol sync vector have to be bit-perfect to
  be decodable, and getting that subtly wrong produces invalid
  transmissions.
- WiFi + NTP (built into the ESP32 Arduino core) for UTC time sync — both
  WSPR and WINK-Beacon need to start transmitting within a couple of
  seconds of an even time-slot boundary.

## What's implemented here

- `wink_beacon.h/.cpp` — direct C++ port of the validated Python
  `wink_beacon.py`: same compact callsign/locator/power packing, same K=9
  rate-1/2 convolutional code, same interleaver. This is WINK's own design,
  not a legacy format, so there's no external spec to get subtly wrong —
  it only has to agree with itself, and it's already validated against
  the Python reference (`test_vectors.txt`, generate with
  `beacon/gen_test_vectors.py`).
- `si5351_tone.h/.cpp` — phase-continuous tone stepping. Naively calling
  `si5351_set_freq()` on every symbol resets the PLL and glitches phase,
  which real WSPR/beacon firmware avoids by only adjusting the output
  divider's fractional part between symbols, never resetting the PLL
  mid-transmission. Implemented per that principle; **verify the actual
  phase continuity on a scope** before trusting it, PLL/multisynth
  register behaviour is easy to get subtly wrong per Si5351 revision.
- `main.cpp` — NTP sync, slot-boundary scheduling, alternates WSPR
  (via JTEncode) and WINK-Beacon (via wink_beacon.cpp) on alternate even
  slots, keys PA-enable GPIO around each transmission.

## What's explicitly NOT here

- PA/LPF hardware design.
- Real over-air validation of either mode.
- Band-plan / regulatory frequency table (`config.h` has placeholders —
  fill in your actual licensed beacon sub-bands).
- WINKMESH — no routing protocol exists yet to put on this hardware.

## Before you transmit

1. Bench-test the Si5351 output on a spectrum analyzer / scope before
   it ever reaches a PA — confirm tone spacing, frequency accuracy
   against a calibrated reference, and phase continuity between symbols.
2. Verify WSPR output against a real decoder (WSJT-X or a KiwiSDR/
   WebSDR you can point at your own signal) before assuming JTEncode
   integration is wired correctly here.
3. Confirm your PA + LPF meets your jurisdiction's spurious-emission
   requirements at 5 W before going on air.
4. Only then validate WINK-Beacon the same way — a receive-side decoder
   for it doesn't exist yet either (see `beacon/` — currently only the
   Python simulation decoder exists, not a real-signal one).
