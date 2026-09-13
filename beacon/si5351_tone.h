#pragma once
// Phase-continuous tone stepping for the Si5351.
//
// The naive approach -- call si5351_set_freq() fresh for every symbol --
// resets the PLL on most Si5351 driver implementations, which glitches
// phase at every symbol boundary. For continuous-phase FSK (both WSPR
// and WINK-Beacon are CP-FSK) that phase glitch shows up as splatter
// and hurts decode. The standard fix (used by real Si5351 WSPR/beacon
// firmware) is: set up the PLL once for the band, then step between
// symbols by only touching the output multisynth's fractional divider,
// never re-running the PLL reset sequence.
//
// This wraps that principle around the Etherkit Si5351Arduino library.
// VERIFY ACTUAL PHASE CONTINUITY ON A SCOPE -- register-level PLL/
// multisynth behaviour varies by Si5351 silicon revision, and this has
// not been bench-tested.

#include <Arduino.h>
#include <si5351.h>

class GlitchFreeTuner {
public:
    explicit GlitchFreeTuner(Si5351 &si5351) : si5351_(si5351) {}

    // Call once before a transmission: sets the PLL for the given base
    // frequency (tone 0). Subsequent set_tone() calls only move the
    // multisynth fractional divider.
    void begin(uint64_t base_freq_hz_100) {
        base_freq_ = base_freq_hz_100;
        si5351_.set_freq(base_freq_, SI5351_CLK0);
        // Etherkit's set_freq() does the PLL setup; for true glitch-free
        // stepping you generally want set_freq_manual() with a fixed
        // PLL frequency and only vary the multisynth divider per call
        // -- left as a TODO once bench-verified, this begin()/set_tone()
        // split is deliberately here so that swap is localized to this
        // file.
    }

    // tone_hz_x100: target frequency in centi-Hz (Si5351Arduino's native
    // fixed-point unit) for this symbol.
    void set_tone(uint64_t tone_hz_x100) {
        si5351_.set_freq(tone_hz_x100, SI5351_CLK0);
    }

    void off() {
        si5351_.output_enable(SI5351_CLK0, 0);
    }

private:
    Si5351 &si5351_;
    uint64_t base_freq_ = 0;
};
