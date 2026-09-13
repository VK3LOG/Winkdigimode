// WINK-Beacon / WSPR dual-mode transmitter -- reference skeleton, see
// README.md "Before you transmit" before this ever keys a PA.
#include <Arduino.h>
#include <WiFi.h>
#include "time.h"
#include <si5351.h>
#include <JTEncode.h>

#include "config.h"
#include "wink_beacon.h"
#include "si5351_tone.h"

Si5351 si5351;
JTEncode jtencode;
GlitchFreeTuner tuner(si5351);

// Symbol timing: 12000/8192 baud, same for WSPR and WINK-Beacon.
const double SYMBOL_PERIOD_MS = 8192.0 / 12000.0 * 1000.0;  // ~682.67 ms

TxMode next_mode = TxMode::WSPR;

void setup_wifi_and_time() {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("Connecting to WiFi");
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.println(" connected.");
    configTime(0, 0, NTP_SERVER);  // UTC
    struct tm tm_now;
    while (!getLocalTime(&tm_now)) {
        Serial.println("Waiting for NTP sync...");
        delay(1000);
    }
    Serial.println("Time synced.");
}

void setup_si5351() {
    si5351.init(SI5351_CRYSTAL_LOAD_8PF, SI5351_REF_XTAL_HZ, 0);
    si5351.set_correction(0, SI5351_PLL_INPUT_XO);  // TODO: calibrate against a
                                                     // reference and set real PPM
                                                     // correction before trusting
                                                     // frequency accuracy
    si5351.output_enable(SI5351_CLK0, 0);
    pinMode(PA_ENABLE_PIN, OUTPUT);
    digitalWrite(PA_ENABLE_PIN, !PA_ENABLE_ACTIVE_HIGH);
}

void pa_enable(bool on) {
    digitalWrite(PA_ENABLE_PIN, on == PA_ENABLE_ACTIVE_HIGH);
}

// Blocks until the start of the next even UTC minute boundary that's a
// valid WSPR/WINK-Beacon slot start (WSPR: even minute, second==0).
void wait_for_slot_boundary() {
    struct tm tm_now;
    for (;;) {
        getLocalTime(&tm_now);
        if (tm_now.tm_min % 2 == 0 && tm_now.tm_sec == 0) return;
        delay(200);
    }
}

void transmit_wspr() {
    Serial.println("TX: WSPR");
    // NOTE: JTEncode's exact method signature has changed across
    // versions (char* vs const char*, argument order) -- check the
    // installed JTEncode.h for wspr_encode()'s real signature and the
    // real symbol-count constant before this will compile.
    uint8_t symbols[JTENCODE_WSPR_SYMBOL_COUNT];
    jtencode.wspr_encode(STATION_CALLSIGN, STATION_LOCATOR, STATION_POWER_DBM, symbols);

    tuner.begin((uint64_t)(WSPR_DIAL_FREQ_HZ_20M + TX_AUDIO_OFFSET_HZ) * 100ULL);
    pa_enable(true);
    for (int i = 0; i < JTENCODE_WSPR_SYMBOL_COUNT; i++) {
        uint64_t tone_hz_x100 =
            (uint64_t)((WSPR_DIAL_FREQ_HZ_20M + TX_AUDIO_OFFSET_HZ +
                        symbols[i] * (12000.0 / 8192.0)) * 100.0);
        tuner.set_tone(tone_hz_x100);
        delay((unsigned long)SYMBOL_PERIOD_MS);
    }
    pa_enable(false);
    tuner.off();
}

void transmit_wink_beacon() {
    Serial.println("TX: WINK-Beacon");
    uint8_t symbols[72];
    int n = winkbeacon::encode_beacon_symbols(STATION_CALLSIGN, STATION_LOCATOR,
                                               STATION_POWER_DBM, symbols);

    // TODO: prepend the fixed 20-symbol sync preamble here (generate
    // with beacon/gen_test_vectors.py, paste as a const array -- must
    // match the RX side exactly, which doesn't exist yet, see README).

    tuner.begin((uint64_t)(WSPR_DIAL_FREQ_HZ_20M + TX_AUDIO_OFFSET_HZ) * 100ULL);
    pa_enable(true);
    for (int i = 0; i < n; i++) {
        uint64_t tone_hz_x100 =
            (uint64_t)((WSPR_DIAL_FREQ_HZ_20M + TX_AUDIO_OFFSET_HZ +
                        symbols[i] * (12000.0 / 8192.0)) * 100.0);
        tuner.set_tone(tone_hz_x100);
        delay((unsigned long)SYMBOL_PERIOD_MS);
    }
    pa_enable(false);
    tuner.off();
}

void setup() {
    Serial.begin(115200);
    setup_wifi_and_time();
    setup_si5351();
}

void loop() {
    wait_for_slot_boundary();
    if (next_mode == TxMode::WSPR) {
        transmit_wspr();
        next_mode = TxMode::WINK_BEACON;
    } else {
        transmit_wink_beacon();
        next_mode = TxMode::WSPR;
    }
}
