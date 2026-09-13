"""WINK rig control via Hamlib -- the rigctld network protocol.

This is how the rest of the ham ecosystem uses Hamlib (WSJT-X, flrig,
CQRLOG all speak rigctld): a line-based TCP protocol, default port
4532. Implemented here in pure Python so the GUI and the portable EXE
need no native libhamlib dependency.

Covered command subset (stable across Hamlib 3.x/4.x rigctld):
  f / F <hz>        get / set frequency (VFOA/current VFO)
  m / M <m> <pb>    get / set mode + passband Hz  (m replies: MODE, PB, RPRT)
  v / V <vfo>       get / set VFO
  t / T <0|1>       get / set PTT
  s / S <s> <vfo>   get / set split (s replies: 0/1, TXVFO, RPRT)
  l <level>         get level, e.g. l STRENGTH -> float 0..1 (rig-reported)
  q                 quit (close)
Every command terminates with a "RPRT <code>" line; 0 means success.

No rigctld running? RigClient.connect() raises RigError with a message
telling the operator exactly what to start, e.g.:
  rigctld -m 3073 -r /dev/ttyUSB0 -s 115200
(model numbers: `rigctl -l`). A FakeRigServer below implements the same
subset in-process so tests and the GUI exercise the real client code
with no hardware attached.
"""

from __future__ import annotations
import socket
import threading


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4532


# Curated Hamlib rig-model catalog, verified against Hamlib 4.6+
# include/hamlib/riglist.h (model = backend*1000 + id). Numbers shift
# between Hamlib releases, so the GUI also accepts a manual model
# number -- and 'rigctl -l' on the user's own machine is always the
# final authority. Dummy(1)/FLRig(4) need no serial hardware.
RIG_MODELS: list[tuple[str, int]] = [
    ("Hamlib Dummy (no radio)", 1),
    ("FLRig (via flrig)", 4),
    ("Icom IC-7300", 3073),
    ("Icom IC-7610", 3078),
    ("Icom IC-9700", 3081),
    ("Icom IC-705", 3085),
    ("Icom IC-7100", 3070),
    ("Icom IC-7600", 3063),
    ("Xiegu G90", 3088),
    ("Yaesu FT-991", 1035),
    ("Yaesu FTDX10", 1042),
    ("Yaesu FTDX101D/MP", 1040),
    ("Yaesu FT-450D", 1046),
    ("Yaesu FT-2000", 1029),
    ("Kenwood TS-590S", 2031),
    ("Kenwood TS-590SG", 2037),
    ("Kenwood TS-2000", 2014),
    ("Kenwood TS-480", 2028),
    ("Kenwood TS-890S", 2041),
    ("Elecraft K3/K3S", 2029),
    ("Elecraft K4", 2047),
    ("Elecraft KX2", 2044),
    ("Elecraft KX3", 2045),
]
COMMON_BAUDS = (4800, 9600, 19200, 38400, 57600, 115200)


def find_rigctld() -> str | None:
    """Path to the rigctld binary, or None if Hamlib isn't installed."""
    import shutil
    return shutil.which("rigctld")


def launch_rigctld(model: int, serial_port: str, baud: int = 9600,
                   extra_args: list[str] | None = None):
    """Start a local rigctld subprocess (WSJT-X does the equivalent
    internally when it opens a rig). Returns the Popen handle; caller
    owns it (terminate on disconnect/app exit). Raises RigError if the
    rigctld binary is missing or exits immediately."""
    import subprocess
    binary = find_rigctld()
    if binary is None:
        raise RigError(
            "rigctld not found -- install Hamlib "
            "(Debian/Ubuntu: sudo apt install libhamlib-utils).")
    if model not in (1, 4) and not serial_port.strip():
        raise RigError("a serial port is required for this rig model "
                       "(Dummy/FLRig are the exceptions).")
    cmd = [binary, "-m", str(int(model)), "-r", serial_port.strip() or "null",
           "-s", str(int(baud)), "-C", "auto_power_on=1"]
    if extra_args:
        cmd.extend(extra_args)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RigError(f"could not start rigctld: {exc}") from exc
    import time
    time.sleep(0.8)
    if proc.poll() is not None:
        raise RigError(
            f"rigctld exited immediately (code {proc.returncode}) -- "
            f"wrong model/serial port? Tried: {' '.join(cmd)}")
    if not probe(DEFAULT_HOST, DEFAULT_PORT, timeout=2.0):
        proc.terminate()
        raise RigError("rigctld started but is not answering on "
                       f"{DEFAULT_HOST}:{DEFAULT_PORT}.")
    return proc


class RigError(Exception):
    """Raised for connection failures and rig-reported errors."""


class RigClient:
    """Blocking rigctld client. One connection per instance; not shared
    across threads (open one per background job instead)."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 3.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._file = None

    # -- connection -------------------------------------------------
    def connect(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise RigError(
                f"no rigctld at {self.host}:{self.port} ({exc}). "
                f"Start one, e.g. 'rigctld -m <model> -r <serial-device>', "
                f"or use the GUI's fake-rig test mode.") from exc
        self._sock.settimeout(self.timeout)
        self._file = self._sock.makefile("r", newline="\n")

    def close(self) -> None:
        try:
            if self._sock is not None:
                try:
                    self._sock.sendall(b"q\n")
                except OSError:
                    pass
        finally:
            try:
                if self._file is not None:
                    self._file.close()
            finally:
                if self._sock is not None:
                    self._sock.close()
                self._sock = None
                self._file = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def __enter__(self) -> "RigClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- protocol ---------------------------------------------------
    def _cmd(self, cmd: str, n_lines: int) -> list[str]:
        if not self.connected:
            raise RigError("not connected")
        try:
            self._sock.sendall((cmd + "\n").encode("ascii"))
            lines = [self._file.readline().rstrip("\n") for _ in range(n_lines)]
        except OSError as exc:
            raise RigError(f"rig link failed on {cmd!r}: {exc}") from exc
        if len(lines) < n_lines or not lines[-1].startswith("RPRT"):
            raise RigError(f"truncated reply to {cmd!r}: {lines!r}")
        try:
            code = int(lines[-1].split()[1])
        except (IndexError, ValueError):
            raise RigError(f"bad RPRT line for {cmd!r}: {lines[-1]!r}")
        if code != 0:
            raise RigError(f"rig rejected {cmd!r} (RPRT {code})")
        return lines[:-1]

    # -- rig operations ----------------------------------------------
    def get_freq(self) -> int:
        (line,) = self._cmd("f", 2)
        return int(float(line))

    def set_freq(self, hz: int) -> None:
        self._cmd(f"F {int(hz)}", 1)

    def get_mode(self) -> tuple[str, int]:
        mode, pb, *_ = self._cmd("m", 3)
        return mode.strip(), int(float(pb))

    def set_mode(self, mode: str, passband_hz: int = 0) -> None:
        self._cmd(f"M {mode} {int(passband_hz)}", 1)

    def get_vfo(self) -> str:
        (line,) = self._cmd("v", 2)
        return line.strip()

    def set_vfo(self, vfo: str) -> None:
        self._cmd(f"V {vfo}", 1)

    def get_ptt(self) -> bool:
        (line,) = self._cmd("t", 2)
        return line.strip() == "1"

    def set_ptt(self, on: bool) -> None:
        self._cmd(f"T {1 if on else 0}", 1)

    def get_split(self) -> tuple[bool, str]:
        lines = self._cmd("s", 3)
        return (lines[0].strip() == "1", lines[1].strip())

    def set_split(self, on: bool, tx_vfo: str = "VFOB") -> None:
        self._cmd(f"S {1 if on else 0} {tx_vfo}", 1)

    def get_strength(self) -> float:
        """Rig-reported S-meter 0..1 via 'l STRENGTH' (scale is rig-
        dependent; treat as relative, not calibrated dB)."""
        (line,) = self._cmd("l STRENGTH", 2)
        return float(line)


def probe(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          timeout: float = 1.0) -> bool:
    """True if something answers like rigctld at host:port."""
    try:
        with RigClient(host, port, timeout) as rig:
            rig.get_freq()
        return True
    except RigError:
        return False


class FakeRigServer:
    """In-process rigctld stand-in implementing the same subset, for
    tests and the GUI's hardware-free demo path. Not a hardware driver
    -- just a predictable peer for the real client code above."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = 0,
                 freq_hz: int = 7084000, mode: str = "USB",
                 passband_hz: int = 2400):
        self.host = host
        self.port = port
        self.freq_hz = freq_hz
        self.mode = mode
        self.passband_hz = passband_hz
        self.vfo = "VFOA"
        self.ptt = False
        self.split = False
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        # shutdown() first: on Linux, close() alone does not wake a
        # thread blocked in accept(), so the server would keep serving.
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _serve(self) -> None:
        while self._sock is not None:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            f = conn.makefile("r", newline="\n")
            while True:
                line = f.readline()
                if not line:
                    return
                parts = line.strip().split()
                if not parts:
                    continue
                cmd, args = parts[0], parts[1:]
                body, code = self._dispatch(cmd, args)
                if body is None:  # q
                    return
                conn.sendall((body + f"RPRT {code}\n").encode("ascii"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, cmd: str, args: list[str]) -> tuple[str | None, int]:
        if cmd == "q":
            return None, 0
        if cmd == "f":
            return f"{self.freq_hz}\n", 0
        if cmd == "F" and args:
            self.freq_hz = int(float(args[0]))
            return "", 0
        if cmd == "m":
            return f"{self.mode}\n{self.passband_hz}\n", 0
        if cmd == "M" and len(args) >= 2:
            self.mode, self.passband_hz = args[0], int(float(args[1]))
            return "", 0
        if cmd == "v":
            return f"{self.vfo}\n", 0
        if cmd == "V" and args:
            self.vfo = args[0]
            return "", 0
        if cmd == "t":
            return f"{1 if self.ptt else 0}\n", 0
        if cmd == "T" and args:
            self.ptt = args[0] == "1"
            return "", 0
        if cmd == "s":
            return f"{1 if self.split else 0}\n{self.vfo}\n", 0
        if cmd == "S" and len(args) >= 2:
            self.split = args[0] == "1"
            return "", 0
        if cmd == "l":
            return "0.42\n", 0
        return "", -1
