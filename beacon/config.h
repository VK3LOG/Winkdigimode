#pragma once
// EDIT ALL OF THIS to match your actual board wiring and licensed
// beacon sub-bands before building. Values below are placeholders.

// ---- station identity ----
#define STATION_CALLSIGN   "VK3LOG"
#define STATION_LOCATOR    "QF22"    // 4-char Maidenhead, update to your actual grid
#define STATION_POWER_DBM  27        // 27 dBm = ~0.5W indicated (WSPR power codes are
                                     // quantized to a standard dBm table -- see JTEncode
                                     // docs for the accepted values before assuming this
                                     // one is valid)

// ---- Si5351 I2C pins (ESP32-S3 defaults; check your board's silkscreen) ----
#define I2C_SDA_PIN   8
#define I2C_SCL_PIN   9
#define SI5351_I2C_ADDR 0x60
#define SI5351_REF_XTAL_HZ 25000000UL  // confirm against YOUR Si5351 module's crystal;
                                        // many breakout boards use 25MHz, some use 27MHz

// ---- PA / RF control ----
#define PA_ENABLE_PIN   4    // drives PA bias/enable -- HIGH = transmitting
#define PA_ENABLE_ACTIVE_HIGH true

// Placeholder band-select relay bank -- wire to your actual LPF bank if
// this beacon is multi-band. Empty/unused if single-band.
#define NUM_BAND_RELAYS 0
// static const int BAND_RELAY_PINS[NUM_BAND_RELAYS] = { };

// ---- frequencies ----
// WSPR/WINK-Beacon "dial frequency" (the base of the ~200 Hz sub-band
// that's actually used) per band. FILL IN your licensed beacon
// sub-bands -- these example values are commonly-used WSPR dial
// frequencies, they are NOT a substitute for checking your own
// license/band plan.
#define WSPR_DIAL_FREQ_HZ_20M   14097000UL
#define WSPR_DIAL_FREQ_HZ_40M    7040000UL

// Actual TX frequency = dial freq + (audio offset within the ~200Hz slot).
// Pick a fixed offset within the WSPR sub-band, or randomize per TX to
// spread across the band (common WSPR practice) -- TODO, not implemented.
#define TX_AUDIO_OFFSET_HZ   1500.0

// ---- WiFi (for NTP time sync -- both modes need to start within ~1-2s
// of an even UTC time-slot boundary) ----
#define WIFI_SSID     "YOUR_SSID"
#define WIFI_PASSWORD "YOUR_PASSWORD"
#define NTP_SERVER    "pool.ntp.org"

// ---- mode schedule ----
// Alternate WSPR and WINK-Beacon on alternate even 2-minute slots by
// default. Change to run only one mode.
enum class TxMode { WSPR, WINK_BEACON };
