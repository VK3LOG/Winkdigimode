# WINK — weak-signal keyboard-to-keyboard modem + monitor

WINK is an amateur-radio digital mode for keyboard-to-keyboard QSOs at
very low SNR, with adaptive S/N/F profiles, a WINK-Beacon mode, mesh
relay support, and an FT8-calibrated sensitivity story. Default channel:
**7.084 MHz**.

## Contents

- Modem: `wink_core.py` (packets/FEC), `wink_modem2.py` (8-FSK PHY),
  `wink_profiles.py` (S/N/F + thresholds), `wink_fading.py` (Rayleigh/
  Rician/two-path channel), `wink_mesh.py`, `wink_wideband.py`
- GUI: `wink_monitor.py` (GTK4/Libadwaita -- stations heard, RX/TX
  boxes, settings in the menu), `wink_engine.py` (background modem
  worker), `wink_rig.py` (Hamlib rigctld client), `audio_devices.py`
  (soundcard enumeration + level meter)
- CLI: `wink_cli.py` (same engine, headless)
- Loopback: `audio_loopback.py` (sound-card validation tool)
- Comparison: `ft8_sim.py` + `ft8_ldpc_tables.py` (real WSJT-X
  LDPC(174,91) frame), `run_ft8_compare.py`
- Benchmarks: `run_bakeoff2.py`, `run_fecv3.py`, `run_margin_sweep.py`,
  `demo_adaptive_qso.py`, `demo_mesh_relay.py` -- measured CSVs in `results/`
- `beacon/` -- beacon firmware (ESP32/Si5351), test vectors, beacon docs
- `docs/` -- `WINK_V02_RESULTS.md` (all measured thresholds + FT8 table),
  `WINKMESH_DESIGN_NOTES.md`, `WIDEBAND_RX.md`
- Prebuilt: `WINK-CLI-linux`, `WINK-CLI-win64.exe`, `WINK-GUI-win64.exe`

## Install

**Debian/Ubuntu (recommended):**

    sudo apt install ./wink-monitor_0.2-2.deb

This installs `wink-monitor` (GUI) and `wink-cli` plus the desktop entry.
It pulls `python3-numpy`, GTK4/Libadwaita bindings and `libhamlib-utils`
(rigctld) automatically.

**From source** (any Linux with Python 3.10+):

    pip install --break-system-packages numpy sounddevice
    sudo apt install libportaudio2 gir1.2-gtk-4.0 gir1.2-adw-1
    python3 wink_monitor.py     # GUI
    python3 wink_cli.py list    # CLI

**Windows:** run `WINK-GUI-win64.exe` (bundled GTK+Python, nothing to
install) or `WINK-CLI-win64.exe` from a terminal. First run
`setup` to create the working files: `WINK-CLI-win64.exe setup`.

## GUI walkthrough

One main page, WSJT-X style:

1. **Stations heard (left).** Scans continuously while TX is idle
   (FT8-style, ~35 s per ±200 Hz pass). Click **Select** on a station
   to set the reply target; if a rig is connected it QSYs to
   dial + offset + measured CFO automatically.
2. **Receive (right, top).** Decoded keyboard traffic, one line per
   message. Misses are logged honestly (`-- no decode @ ...`).
3. **TX box (right).** Type (max 32 chars) + **Send**. In simulated
   mode pick a station first; in soundcard mode it goes out as CQ
   unless a station is selected.
4. **Controls row:** profile (WINK-S/N/F) | channel (awgn/fading) |
   FEC (K9/K9_1_3) | link SNR in dB (simulated path) |
   path (simulated/soundcard).
5. **Listen button** (left, under Scan): starts the real soundcard RX
   listener -- anything decoded off the air lands in Receive.
6. **Menu:** Station & Rig (callsign, dial, rigctld host/port, rig
   model + serial + baud, Launch rigctld, fake-rig demo mode),
   Audio (device pick, Test, live input level bar), About.
7. **Status bar:** rig freq/mode, last decode, profile/channel/FEC.

## CLI reference

    wink-cli list                          # profiles, thresholds, FEC codes
    wink-cli setup                         # create ./wink_out workdir (idempotent)
    wink-cli scan [--snr -10] [--profile WINK-S]
    wink-cli send "hello" --to VK3ABC --snr -14 --profile WINK-N
    wink-cli demo [--ultra]                # full adaptive S/N/F QSO
    wink-cli fec [--trials 20]             # K9 vs K9_1_3 comparison + CSV
    wink-cli beacon                        # beacon encode + threshold check
    wink-cli next -15 WINK-S               # what would recommend_profile do?
    wink-cli rig probe                     # is rigctld answering?
    wink-cli rig freq [--set 7084000]      # read/set frequency
    wink-cli rig mode | ptt --state on | smeter

`send --to` accepts a callsign (hashed to an id) or a hex id; default
is the `0xFFFFFFFF` CQ broadcast.

## Radio setup (Hamlib, WSJT-X-style)

1. GUI: menu -> Station & Rig. Pick your **rig model** (numbers verified
   against Hamlib 4.6; `rigctl -l` on your machine is authoritative, and
   a manual-number override is provided), serial port and baud.
2. Press **Launch rigctld** (starts the daemon for you), then **Connect**.
   Frequency/mode appear in the status bar; the wideband Reply button
   drives the rig.
3. No radio handy? **Start fake rig** gives you an in-process rigctld
   stand-in so every control can be exercised end to end.

## Audio setup

1. Install the backend: `sudo apt install libportaudio2`, then
   `pip install --break-system-packages sounddevice`.
2. Menu -> Audio: pick input/output, press **Test** (checks 12 kHz mono
   natively, else confirms the software resampler path), watch the
   **input level bar** -- aim 0.3-0.7 like WSJT-X's green zone.
3. Loopback self-test without a second station: create a monitor route
   (`pactl load-module module-null-sink sink_name=wink &&` record from
   `Monitor of ...`), or a physical cable, then:
   `python3 audio_loopback.py --mock -12` (toolchain check, no hardware)
   or the full GUI Listen + Send path.
4. Set the path selector to **soundcard**; Send keys the real output,
   Listen decodes the real input. Nothing has been proven on RF yet --
   treat first on-air tests as experiments, not demonstrations.

## Reproducing the measurements

    python3 run_bakeoff2.py --channel rayleigh --fd 1 --profile S --snrs -24 -22 -20 --trials 24
    python3 run_fecv3.py WINK-S              # K9 vs K9_1_3
    python3 run_ft8_compare.py --channel awgn|rayleigh
    python3 run_margin_sweep.py              # adaptation margin 1-5 dB
    python3 demo_adaptive_qso.py [--ultra] [--fade]
    python3 demo_mesh_relay.py               # line, diamond, broadcast topologies

All SNRs are mean-power-over-burst; fading runs use average received
power, so deep fades punch through at high SNR -- that's the point.

## Rebuilding the packaged apps

- Linux CLI: `pyinstaller --onefile --name WINK-CLI --paths . wink_cli.py`
- Windows CLI: same under Wine with Windows Python 3.12 + numpy 1.26.4
  (newer numpy calls UCRT functions Wine 10 lacks).
- Windows GUI: Wine Python 3.12 + gvsbuild 2024.10.0 runtime
  (cp312 PyGObject wheels) + PyInstaller with the GTK DLLs, typelibs
  (`gi_typelibs/` -- PyInstaller's own hook overwrites GI_TYPELIB_PATH),
  GSettings schemas and Adwaita icons bundled.
- Deb: see `debian/` notes in release history; `dpkg-deb --build`.

## Measured sensitivity (K9 + soft LLRs, 50% packet success)

| mode | AWGN | Rayleigh fd=1 Hz |
|---|---|---|
| FT8 (12.64 s, 50 Hz) | -23.0 dB | -20.3 dB |
| WINK-S (~33 s, 60 Hz) | -24.1 dB | -21.6 dB |
| WINK-N | -19.3 dB | -17.3 dB |
| WINK-F | -16.4 dB | -14.2 dB |

Rate-1/3 ULTRA FEC (`K9_1_3`) adds ~1.3 dB at 1.5x airtime. Full story,
caveats and next steps: `docs/WINK_V02_RESULTS.md`.
