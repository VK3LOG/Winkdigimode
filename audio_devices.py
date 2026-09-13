"""Real sound-card enumeration via sounddevice (PortAudio bindings).

Not mocked -- this actually queries the host's audio backend. Degrades
gracefully (empty lists + a clear reason) if sounddevice isn't
installed or PortAudio's native library isn't present, which is a real
case: sounddevice's own import raises OSError (not ImportError) when
libportaudio is missing, so both need catching.
"""

try:
    import sounddevice as sd
    AVAILABLE = True
    UNAVAILABLE_REASON = None
except OSError as e:
    AVAILABLE = False
    UNAVAILABLE_REASON = (
        "PortAudio library not found. On Debian/Ubuntu: "
        "sudo apt install libportaudio2"
    )
except ImportError:
    AVAILABLE = False
    UNAVAILABLE_REASON = "sounddevice not installed. pip install sounddevice"


def _query():
    """Returns the raw device list with each entry's original global
    index attached, or [] on any failure (e.g. a backend that's present
    but has no devices, or a permissions issue) -- callers should treat
    [] and 'unavailable' as the same UI state (show the empty/error
    state), not crash.

    The index has to be captured here, before any filtering -- it's
    PortAudio's global device index, not the position within a
    subsequently-filtered input-only or output-only list, and
    check_input_settings()/check_output_settings() need the real one.
    """
    if not AVAILABLE:
        return []
    try:
        devices = list(sd.query_devices())
    except Exception:
        return []
    for i, d in enumerate(devices):
        d["index"] = i
    return devices


def list_input_devices():
    return [d for d in _query() if d.get("max_input_channels", 0) > 0]


def list_output_devices():
    return [d for d in _query() if d.get("max_output_channels", 0) > 0]


def default_input_index():
    if not AVAILABLE:
        return None
    try:
        idx = sd.default.device[0]
        return idx if idx is not None and idx >= 0 else None
    except Exception:
        return None


def default_output_index():
    if not AVAILABLE:
        return None
    try:
        idx = sd.default.device[1]
        return idx if idx is not None and idx >= 0 else None
    except Exception:
        return None


def test_input_device(device_index: int) -> tuple[bool, str]:
    """WSJT-X-style device check: report the device's native rate and
    whether WINK's 12000 Hz mono path works -- natively, or via our
    software resampler (audio_loopback.resample), which is how WSJT-X
    itself handles non-12kHz hardware."""
    if not AVAILABLE:
        return False, "sounddevice unavailable"
    try:
        dev = sd.query_devices(device_index)
        native = int(dev.get("default_samplerate", 0) or 0)
    except Exception as e:
        return False, str(e)
    try:
        sd.check_input_settings(device=device_index, samplerate=12000, channels=1)
        return True, f"12kHz mono input OK (native {native} Hz)"
    except Exception:
        pass
    try:
        sd.check_input_settings(device=device_index, samplerate=native or 48000, channels=1)
        return True, f"native {native} Hz mono OK (WINK resamples to 12kHz)"
    except Exception as e:
        return False, str(e)


def test_output_device(device_index: int) -> tuple[bool, str]:
    """Same as test_input_device, for the TX path."""
    if not AVAILABLE:
        return False, "sounddevice unavailable"
    try:
        dev = sd.query_devices(device_index)
        native = int(dev.get("default_samplerate", 0) or 0)
    except Exception as e:
        return False, str(e)
    try:
        sd.check_output_settings(device=device_index, samplerate=12000, channels=1)
        return True, f"12kHz mono output OK (native {native} Hz)"
    except Exception:
        pass
    try:
        sd.check_output_settings(device=device_index, samplerate=native or 48000, channels=1)
        return True, f"native {native} Hz mono OK (WINK resamples from 12kHz)"
    except Exception as e:
        return False, str(e)


def input_level(device_index: int, seconds: float = 0.5,
                samplerate: int = 12000) -> tuple[bool, float, str]:
    """WSJT-X-style input thermometer: record briefly and return peak
    0..1 (fraction of full scale). Guides the operator's mic/RF-gain
    setting the same way WSJT-X's green level bar does."""
    if not AVAILABLE:
        return False, 0.0, "sounddevice unavailable"
    try:
        import numpy as np
        frames = int(seconds * samplerate)
        rec = sd.rec(frames, samplerate=samplerate, channels=1,
                     device=device_index, blocking=True)
        peak = float(np.max(np.abs(np.asarray(rec, dtype=float))))
        return True, min(1.0, peak), f"peak {peak:.2f} of full scale"
    except Exception as e:
        return False, 0.0, str(e)
