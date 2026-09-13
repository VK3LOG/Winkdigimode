#pragma once
// Direct port of wink_beacon.py's encode path. This is WINK's own
// design (not a legacy format), so correctness here means "matches the
// Python reference" -- verify with beacon/gen_test_vectors.py before
// trusting this on air.
#include <Arduino.h>
#include <vector>

namespace winkbeacon {

// ---- CRC32 (same construction as Python's zlib.crc32, truncated to 8 bits) ----
inline uint32_t crc32_update(uint32_t crc, const uint8_t *data, size_t len) {
    crc = ~crc;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++)
            crc = (crc >> 1) ^ (0xEDB88320UL & (-(int32_t)(crc & 1)));
    }
    return ~crc;
}

// ---- compact callsign/locator/power packing (matches wink_beacon.py) ----
const char CALLSIGN_ALPHABET[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ";  // 37 symbols

inline uint32_t pack_callsign(const char *cs_in, int length = 6) {
    char cs[8];
    int n = strlen(cs_in);
    for (int i = 0; i < length; i++)
        cs[i] = (i < n) ? toupper(cs_in[i]) : ' ';
    uint32_t v = 0;
    for (int i = 0; i < length; i++) {
        const char *p = strchr(CALLSIGN_ALPHABET, cs[i]);
        int idx = p ? (p - CALLSIGN_ALPHABET) : 36;
        v = v * 37 + idx;
    }
    return v;  // 37^6 ~= 2.57e9, fits uint32
}

inline uint16_t pack_locator(const char *loc) {
    int A = toupper(loc[0]) - 'A';
    int B = toupper(loc[1]) - 'A';
    int C = loc[2] - '0';
    int D = loc[3] - '0';
    return ((A * 18 + B) * 10 + C) * 10 + D;  // 0..32399, fits uint16
}

// raw[8]: 4 bytes callsign (big-endian) + 2 bytes locator (big-endian)
// + 1 byte power + 1 byte CRC8. 64 bits total, matches wink_beacon.py
// encode_beacon().
inline void encode_beacon(const char *callsign, const char *locator, int power_dbm,
                           uint8_t raw_out[8]) {
    uint32_t cs = pack_callsign(callsign);
    uint16_t loc = pack_locator(locator);
    uint8_t pw = (uint8_t)constrain(power_dbm, 0, 63);

    raw_out[0] = (cs >> 24) & 0xFF;
    raw_out[1] = (cs >> 16) & 0xFF;
    raw_out[2] = (cs >> 8) & 0xFF;
    raw_out[3] = cs & 0xFF;
    raw_out[4] = (loc >> 8) & 0xFF;
    raw_out[5] = loc & 0xFF;
    raw_out[6] = pw;
    raw_out[7] = crc32_update(0, raw_out, 7) & 0xFF;
}

// ---- K=9 rate-1/2 convolutional encoder (Voyager-standard, generators 753/561 octal) ----
// Matches wink_core.py's FEC_CONFIGS["K9"].
const int K9 = 9;
const uint32_t K9_G0 = 0753;  // octal
const uint32_t K9_G1 = 0561;

inline int parity32(uint32_t x) {
    x ^= x >> 16; x ^= x >> 8; x ^= x >> 4;
    return (0x6996 >> (x & 0xF)) & 1;  // parity lookup nibble trick
}

// bits_in: raw message bits (MSB-first per byte), nbits long.
// Appends (K9-1) zero tail bits before encoding (same as conv_encode(terminate=True)).
// Writes 2*(nbits + K9 - 1) coded bits into coded_out (caller must size it).
inline int conv_encode_k9(const uint8_t *bits_in, int nbits, uint8_t *coded_out) {
    uint32_t state = 0;
    int out_i = 0;
    int total = nbits + (K9 - 1);
    for (int i = 0; i < total; i++) {
        int b = (i < nbits) ? bits_in[i] : 0;
        uint32_t reg = (state << 1) | b;
        coded_out[out_i++] = parity32(reg & K9_G0);
        coded_out[out_i++] = parity32(reg & K9_G1);
        state = reg & ((1u << (K9 - 1)) - 1);
    }
    return out_i;  // == 2*total
}

// ---- interleaver (8-row block interleaver, matches wink_core.interleave) ----
inline void interleave(const uint8_t *bits_in, int n, uint8_t *out, int rows = 8) {
    int cols = (n + rows - 1) / rows;
    std::vector<uint8_t> padded(rows * cols, 0);
    memcpy(padded.data(), bits_in, n);
    int k = 0;
    for (int c = 0; c < cols; c++)
        for (int r = 0; r < rows; r++)
            out[k++] = padded[r * cols + c];
}

// bits_to_symbols_gray, M=4 (2 bits/symbol) -- WINK-Beacon uses 4-FSK,
// same M as WSPR.
inline int bits_to_gray_symbols(const uint8_t *bits, int n, uint8_t *symbols_out) {
    int si = 0;
    for (int i = 0; i + 1 < n; i += 2) {
        int v = (bits[i] << 1) | bits[i + 1];
        symbols_out[si++] = v ^ (v >> 1);  // gray encode
    }
    if (n % 2) {
        int v = bits[n - 1] << 1;
        symbols_out[si++] = v ^ (v >> 1);
    }
    return si;
}

// Full pipeline: callsign/locator/power -> tone-index symbol array ready
// for the Si5351. sync_prefix should be prepended by the caller (same
// pseudo-random sync used in wink_beacon.py's make_sync(M=4, length=20)
// -- generate once with beacon/gen_test_vectors.py and paste the fixed
// array here as a constant; it must match the RX side exactly).
inline int encode_beacon_symbols(const char *callsign, const char *locator, int power_dbm,
                                  uint8_t symbols_out[72]) {
    uint8_t raw[8];
    encode_beacon(callsign, locator, power_dbm, raw);

    uint8_t bits[64];
    for (int i = 0; i < 8; i++)
        for (int b = 0; b < 8; b++)
            bits[i * 8 + b] = (raw[i] >> (7 - b)) & 1;

    uint8_t coded[2 * (64 + K9 - 1)];  // 144 bits
    int coded_len = conv_encode_k9(bits, 64, coded);

    uint8_t shuffled[2 * (64 + K9 - 1)];
    interleave(coded, coded_len, shuffled);

    return bits_to_gray_symbols(shuffled, coded_len, symbols_out);  // 72 symbols
}

}  // namespace winkbeacon
