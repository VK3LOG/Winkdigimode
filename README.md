# WINK — weak-signal keyboard-to-keyboard modem + monitor

WINK is an amateur-radio digital mode for keyboard-to-keyboard QSOs at
very low SNR, with adaptive S/N/F profiles, a WINK-Beacon mode, mesh
relay support, and an FT8-calibrated sensitivity story. Default channel:
**7.084 MHz**.

## Layout (flat -- everything runs from this folder)

- Modem: `wink_core.py` (packets/FEC), `wink_modem2.py` (8-FSK PHY),
  `wink_profiles.py` (S/N/F + thresholds), `wink_fading.py` (Rayleigh/
  Rician/two-path channel), `wink_mesh.py`, `wink_wideband.py`
- Apps: `wink_monitor.py` (GTK4/Libadwaita GUI -- stations heard, RX/TX
  boxes, settings in the menu), `audio_loopback.py` (sound-card validation)
- Comparison: `ft8_sim.py` + `ft8_ldpc_tables.py` (real WSJT-X
  LDPC(174,91) frame), `run_ft8_compare.py`
- Benchmarks: `run_bakeoff2.py`, `run_fecv3.py`, `demo_adaptive_qso.py`,
  `demo_mesh_relay.py` -- measured CSVs alongside
- `beacon/` -- beacon firmware (ESP32/Si5351), test vectors, beacon docs
- Docs: `WINK_V02_RESULTS.md` (all measured thresholds + FT8 table)

## Install (Debian/Ubuntu)

    sudo apt install ./wink-monitor_0.2-2.deb

Optional: sound via `sudo apt install libportaudio2`
then `pip install --break-system-packages sounddevice`;
real rig control via the auto-installed `rigctld` (`libhamlib-utils`).

## Run it

GUI (Linux, needs GTK4 + Libadwaita + numpy):

    python3 wink_monitor.py

CLI (portable -- needs nothing):

    ./WINK-CLI-linux list | setup | demo | fec | beacon | next | rig
    WINK-CLI-win64.exe ...            # same on Windows
    WINK-GUI-win64.exe                # Windows GUI (needs real Windows;
                                      # DSP paths can't execute under Wine)

Rig control speaks Hamlib `rigctld` (`rig probe|freq|mode|ptt|smeter`);
pick a model in Station & Rig or launch `rigctld -m <model> -r <port>`.

## Measured sensitivity (K9 + soft LLRs, 50% packet success)

| mode | AWGN | Rayleigh fd=1 Hz |
|---|---|---|
| FT8 (12.64 s, 50 Hz) | -23.0 dB | -20.3 dB |
| WINK-S (~33 s, 60 Hz) | -24.1 dB | -21.6 dB |
| WINK-N | -19.3 dB | -17.3 dB |
| WINK-F | -16.4 dB | -14.2 dB |

Rate-1/3 ULTRA FEC (`K9_1_3`) adds ~1.3 dB at 1.5x airtime. Full story,
caveats and next steps: `WINK_V02_RESULTS.md`.
