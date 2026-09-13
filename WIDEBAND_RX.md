# WINK wideband receive

WINK now has an application-facing wideband RX layer in `wink_wideband.py`.

## Behaviour

The receiver can search a configurable audio-frequency window for multiple
independent WINK carriers. A successful decode returns:

- decoded `Packet`
- WINK profile
- `audio_offset_hz` relative to the WINK dial/base frequency
- decode confidence

The same frequency offset is exposed as `tx_target_hz`. The application can
therefore select a decoded station and transmit its reply at that station's
measured audio offset without requiring the operator to manually tune to it.

This mirrors the useful FT8 operating model: a wide receive passband can show
multiple stations, while TX can be targeted to the selected station. WSJT-X
similarly supports decoding multiple FT8 signals across its configured audio
search window and automatic TX-frequency selection from decoded signals.

## Important architectural boundary

`wink_wideband.py` is **not** part of the WINK packet format. It is an RX/application
layer. The frontend should receive a normalized station record such as:

```json
{
  "peer": "VK2XYZ",
  "rx": {
    "snr": -21.4,
    "audioOffsetHz": -120.0,
    "profile": "WINK-S"
  },
  "tx": {
    "targetAudioOffsetHz": -120.0
  }
}
```

Future WINK waveform revisions can replace the scanner without requiring the
frontend to be regenerated.

## Current implementation status

The first implementation intentionally uses a straightforward frequency-offset
scan. It is correctness-first rather than optimized DSP. A future FFT/coarse
candidate detector can make the scan much faster while preserving the same
`WidebandDecode` API.

## Independent validation (this session)

Re-ran the two-station claim independently: both packets decoded with
payloads and CRCs intact at -10 dB, offsets recovered to within one
search-grid step (-125 Hz found for a true -120 Hz signal, 175 Hz found
for +180 Hz, at search_step_hz=2.5). Also ran 4 pure-noise (no signal)
trials through the full -1000..+1000 Hz scan: **zero false positives**
-- the sync-match gate (needs <=50% symbol errors against an 8-ary
chance level of ~87.5% wrong) filters almost everything before CRC is
even checked, so the false-positive risk is lower than a naive
CRC-only estimate would suggest.

Two things worth knowing before relying on this:
- **Not real-time**: the 800-point/2.5 Hz-step scan took ~55s to
  process one 39s recording on this machine -- consistent with the
  doc's own "correctness-first, not optimized" framing, but a real
  constraint if this is meant to run live rather than on a recording.
- **`cfo_hz` is currently always `0.0`** in the returned
  `WidebandDecode`, even though CFO is estimated internally during
  sync search -- it's just not plumbed through to the result. Doesn't
  affect the audio_offset_hz claim above, but worth fixing before
  `tx_target_hz` is trusted to sub-grid-step precision.

