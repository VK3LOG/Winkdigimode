#!/usr/bin/env python3
"""WINK Monitor -- GTK4/Libadwaita desktop app for the WINK-Beacon /
WINKMESH ecosystem.

STATUS: live UI driven by the real modem. Spots come from genuine
WINK-Beacon TX->channel->RX round trips (wink_engine.beacon_trial),
Wideband runs the real multi-station scan, TX Control drives the real
recommend_profile() with selectable channel/FEC tables, and the Station
page talks to real hardware through Hamlib rigctld (wink_rig) -- with
an in-process fake-rig mode when no radio is attached.

Main page (QsoPage): stations-heard list, RX text log, TX box. All
settings (station/rig/audio) live behind the hamburger menu.

Six views -- all visible at once, WSJT-X style:
  - Left top: band activity (real wideband scan + reply-via-rig)
  - Left bottom: spot log of real beacon trial results
  - Right (scrolls): TX control, station+rig, mesh relay
  - Bottom: status bar (rig freq/mode, last decode, profile/channel/FEC)

AppState is the one shared source of truth (current SNR, active
profile, spot/relay counts, rig connection) so the live data tells one
coherent story across tabs instead of each page silently rolling its
own dice.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio

import os, sys, random
from datetime import datetime, timezone

import audio_devices
import wink_engine
import wink_rig

# Same folder since the flatten (no v02/ anymore): import the real,
# validated thresholds/adaptation logic -- AWGN, fading and ULTRA
# tables -- never a hardcoded copy.
from wink_profiles import (
    THRESHOLDS, THRESHOLDS_FADING, THRESHOLDS_STRONG, ORDER,
    recommend_profile, DEFAULT_FEC, STRONG_FEC,
)


class AppState:
    """Single shared source of live truth. Every page reads from this
    instead of inventing its own random numbers, and calls notify()
    after changing it so subscribers can refresh."""

    def __init__(self):
        self.callsign = "VK3LOG"
        self.locator = "QF22"
        self.current_snr = -14.0      # link SNR control (dB)
        self.current_profile = "WINK-N"
        self.channel = "awgn"          # awgn | fading (threshold table)
        self.fec = DEFAULT_FEC         # K9 | K9_1_3
        self.last_tx_mode = "WSPR"
        self.last_decode_ok = None
        self.last_rx_text = None
        self.audio_input_name = None
        self.audio_output_name = None
        # QSO session state
        self.seq = 0
        self.sel_dest = None           # selected station source-id (int)
        self.sel_offset_hz = 0.0
        self.sel_label = "(no station selected)"
        # Rig (Hamlib rigctld via wink_rig; fake-rig test mode if None host)
        self.rig_host = "127.0.0.1"
        self.rig_port = 4532
        self.rig_connected = False
        self.rig_freq_hz = None
        self.rig_mode = None
        self.dial_freq_hz = 7084000    # WINK channel: 7.084 MHz; wideband offsets add to this
        self._fake_rig = None
        self._rigctld_proc = None
        self._listeners = []

    def subscribe(self, callback):
        self._listeners.append(callback)

    def notify(self):
        for cb in self._listeners:
            cb()

    @property
    def my_id(self) -> int:
        from wink_core import callsign_id
        try:
            return callsign_id(self.callsign)
        except ValueError:
            return 0

    def record_decode(self, text: str, snr_db: float):
        self.current_snr = snr_db
        self.current_profile = recommend_profile(
            self.current_snr, self.current_profile, channel=self.channel)
        self.last_rx_text = text
        self.last_decode_ok = True
        self.notify()

    def record_miss(self, snr_db):
        self.last_decode_ok = False
        self.notify()

    def select_station(self, dest_id, offset_hz, label):
        self.sel_dest = dest_id
        self.sel_offset_hz = offset_hz
        self.sel_label = label
        self.notify()

    # -- rig (Hamlib rigctld) --------------------------------------
    def rig_connect(self) -> tuple[bool, str]:
        """Connect to rigctld; returns (ok, message)."""
        try:
            with wink_rig.RigClient(self.rig_host, self.rig_port) as rig:
                self.rig_freq_hz = rig.get_freq()
                self.rig_mode, _pb = rig.get_mode()
            self.rig_connected = True
            self.notify()
            return True, f"connected: {self.rig_freq_hz} Hz {self.rig_mode}"
        except wink_rig.RigError as exc:
            self.rig_connected = False
            self.notify()
            return False, str(exc)

    def rig_disconnect(self):
        self.rig_connected = False
        self.rig_freq_hz = None
        self.rig_mode = None
        self.notify()

    def rig_set_freq(self, hz: int) -> tuple[bool, str]:
        try:
            with wink_rig.RigClient(self.rig_host, self.rig_port) as rig:
                rig.set_freq(hz)
                self.rig_freq_hz = rig.get_freq()
            self.notify()
            return True, f"rig now on {self.rig_freq_hz} Hz"
        except wink_rig.RigError as exc:
            return False, str(exc)

    def start_fake_rig(self) -> int:
        """Hardware-free demo path: in-process rigctld stand-in. Returns
        the port it listens on (also pointed at by rig_host/port)."""
        if self._fake_rig is None:
            self._fake_rig = wink_rig.FakeRigServer(freq_hz=self.dial_freq_hz)
            port = self._fake_rig.start()
            self.rig_host = "127.0.0.1"
            self.rig_port = port
        return self.rig_port


class BoundedListBox(Gtk.Box):
    """A Gtk.ListBox wrapped in a scroller, with an explicit Python-side
    row count -- Gtk.ListBox doesn't support len()/iteration in the
    GTK4 bindings the way GTK3's get_children() did, so tracking count
    via list(self.list_box) silently breaks. An Adw.StatusPage empty
    state is swapped in place of the scroller while there are zero
    rows."""

    def __init__(self, empty_title: str, empty_description: str, max_rows: int = 200):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.max_rows = max_rows
        self._rows = []

        self.list_box = Gtk.ListBox(css_classes=["boxed-list"])
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_child(self.list_box)

        self.empty_state = Adw.StatusPage(
            title=empty_title, description=empty_description,
            icon_name="network-wireless-acquiring-symbolic", vexpand=True,
        )

        self._stack = Gtk.Stack()
        self._stack.add_named(self.empty_state, "empty")
        self._stack.add_named(self.scroller, "list")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

    def prepend(self, row: Gtk.Widget):
        self.list_box.prepend(row)
        self._rows.insert(0, row)
        while len(self._rows) > self.max_rows:
            oldest = self._rows.pop()
            self.list_box.remove(oldest)
        self._stack.set_visible_child_name("list")

    def clear(self):
        for row in list(self._rows):
            self.list_box.remove(row)
        self._rows.clear()
        self._stack.set_visible_child_name("empty")


###QSOPAGE###
class QsoPage(Gtk.Box):
    """Keyboard-to-keyboard main page: stations heard (left), RX text
    log + TX box (right). Selecting a station sets the reply target
    (destination id + audio offset); Send transmits a real TEXT packet
    through the simulated channel and prints the genuine outcome --
    decoded text or an honest miss."""

    def __init__(self, state: AppState, toast_overlay: Adw.ToastOverlay):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.state = state
        self.toast_overlay = toast_overlay
        self._busy_scan = False
        self._busy_tx = False

        # -- left: stations heard ----------------------------------
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(300, -1)
        left_hdr = Adw.PreferencesGroup(
            title="Stations heard",
            description="Scanning continuously while TX is idle (FT8-style)",
        )
        left.append(left_hdr)
        self.stations = BoundedListBox("No stations yet", "Run a scan to hear the band")
        left.append(self.stations)
        scan_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.snr_combo = Gtk.DropDown(model=Gtk.StringList.new(["-16 dB", "-10 dB", "-4 dB"]),
                                      selected=1)
        scan_row.append(self.snr_combo)
        self.scan_btn = Gtk.Button(label="Scan", css_classes=["suggested-action"], hexpand=True)
        self.scan_btn.connect("clicked", self._on_scan)
        scan_row.append(self.scan_btn)
        left.append(scan_row)
        self.append(left)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # -- right: RX log + TX ------------------------------------
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        rx_hdr = Adw.PreferencesGroup(
            title="Receive",
            description="Decoded keyboard traffic. Simulated channel -- "
                        "misses at low SNR are real decoder failures.",
        )
        right.append(rx_hdr)
        self.rx_view = Gtk.TextView(editable=False, cursor_visible=False,
                                    monospace=True, vexpand=True,
                                    wrap_mode=Gtk.WrapMode.WORD_CHAR)
        rx_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=220)
        rx_scroll.set_child(self.rx_view)
        right.append(rx_scroll)

        self.target_lbl = Gtk.Label(label="To: (no station selected -- scan first)",
                                    halign=Gtk.Align.START)
        right.append(self.target_lbl)

        tx_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.tx_entry = Gtk.Entry(placeholder_text="Type message (max 32 chars)...",
                                  hexpand=True, max_length=32)
        self.tx_entry.connect("activate", lambda *_: self._on_send())
        tx_row.append(self.tx_entry)
        self.send_btn = Gtk.Button(label="Send", css_classes=["suggested-action"])
        self.send_btn.connect("clicked", lambda *_: self._on_send())
        tx_row.append(self.send_btn)
        right.append(tx_row)

        ctl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.profile_combo = Gtk.DropDown(model=Gtk.StringList.new(list(ORDER)),
                                          selected=list(ORDER).index(state.current_profile))
        self.profile_combo.connect("notify::selected", self._on_profile)
        ctl_row.append(self.profile_combo)
        self.chan_combo = Gtk.DropDown(model=Gtk.StringList.new(["awgn", "fading"]))
        self.chan_combo.connect("notify::selected", self._on_channel)
        ctl_row.append(self.chan_combo)
        self.fec_combo = Gtk.DropDown(model=Gtk.StringList.new([DEFAULT_FEC, STRONG_FEC]))
        self.fec_combo.connect("notify::selected", self._on_fec)
        ctl_row.append(self.fec_combo)
        self.snr_spin = Gtk.SpinButton.new_with_range(-30, 0, 1)
        self.snr_spin.set_value(state.current_snr)
        self.snr_spin.set_tooltip_text("Link SNR (dB)")
        self.snr_spin.connect("value-changed", self._on_snr)
        ctl_row.append(self.snr_spin)
        right.append(ctl_row)
        self.append(right)

        state.subscribe(self._refresh_target)
        self._refresh_target()

        # FT8-style: scan at startup, then keep scanning forever
        # whenever TX is idle -- the next pass chains off the previous
        # one's completion. WINK_NO_AUTOSCAN=1 skips the startup scan
        # (headless/Wine smoke tests where numpy DSP is unavailable).
        if not os.environ.get("WINK_NO_AUTOSCAN"):
            GLib.timeout_add_seconds(2, self._auto_scan)

    def _auto_scan(self):
        self._on_scan()
        return False

    def _chain_scan(self):
        if self._busy_tx:
            GLib.timeout_add_seconds(5, self._chain_scan)
        else:
            self._on_scan()
        return False

    # -- RX log ------------------------------------------------------
    def _log(self, line: str):
        buf = self.rx_view.get_buffer()
        buf.insert(buf.get_end_iter(), line + "\n")
        adj = self.rx_view.get_parent().get_vadjustment()
        adj.set_value(adj.get_upper())

    # -- stations ----------------------------------------------------
    def _on_scan(self, *_):
        if self._busy_scan:
            return
        self._busy_scan = True
        self.scan_btn.set_sensitive(False)
        self.scan_btn.set_label("Scanning...")
        self.stations.clear()
        snr = (-16.0, -10.0, -4.0)[self.snr_combo.get_selected()]
        wink_engine.run_async(
            lambda: wink_engine.wideband_trial(snr, self.state.current_profile,
                                               fec=self.state.fec),
            self._on_scan_done)

    def _on_scan_done(self, res):
        self._busy_scan = False
        self.scan_btn.set_sensitive(True)
        self.scan_btn.set_label("Scan")
        if not res.get("ok"):
            self.toast_overlay.add_toast(Adw.Toast(
                title=f"Scan failed: {res.get('error', '?')}", timeout=4))
        elif not res["stations"]:
            self._log(f"-- scan @ {res['snr_db']:.0f} dB: nothing heard --")
        else:
            for st in res["stations"]:
                label = f"0x{st['source']:08x} \u00b7 {st['offset_hz']:+.0f} Hz"
                try:
                    payload = st["payload"].decode(errors="replace")
                except Exception:
                    payload = repr(st["payload"])
                row = Adw.ActionRow(title=label,
                                    subtitle=f"\u201c{payload}\u201d \u00b7 conf {st['confidence']:.0%}")
                sel_btn = Gtk.Button(label="Select", valign=Gtk.Align.CENTER, css_classes=["flat"])
                sel_btn.connect("clicked", self._on_select, st)
                row.add_suffix(sel_btn)
                self.stations.prepend(row)
            self._log(f"-- scan @ {res['snr_db']:.0f} dB: {len(res['stations'])} station(s) "
                      f"in {res['elapsed_s']:.0f}s --")
        # FT8-style: chain the next pass whenever TX is idle.
        GLib.timeout_add_seconds(2, self._chain_scan)

    def _on_select(self, _btn, st):
        try:
            payload = st["payload"].decode(errors="replace")
        except Exception:
            payload = ""
        self.state.select_station(st["source"], st["offset_hz"],
                                  f"0x{st['source']:08x} ({payload}) @ {st['offset_hz']:+.0f} Hz")
        if self.state.rig_connected:
            target = int(self.state.dial_freq_hz + st["offset_hz"])
            wink_engine.run_async(
                lambda: self.state.rig_set_freq(target),
                lambda r: self.toast_overlay.add_toast(Adw.Toast(title=r[1], timeout=3)))

    def _refresh_target(self):
        self.target_lbl.set_label(f"To: {self.state.sel_label}")

    # -- TX ----------------------------------------------------------
    def _on_profile(self, combo, _pspec):
        self.state.current_profile = list(ORDER)[combo.get_selected()]
        self.state.notify()

    def _on_channel(self, combo, _pspec):
        self.state.channel = ("awgn", "fading")[combo.get_selected()]
        self.state.notify()

    def _on_fec(self, combo, _pspec):
        self.state.fec = (DEFAULT_FEC, STRONG_FEC)[combo.get_selected()]
        self.state.notify()

    def _on_snr(self, spin):
        self.state.current_snr = float(spin.get_value())

    def _on_send(self):
        if self._busy_tx:
            return
        text = self.tx_entry.get_text().strip()
        if not text:
            return
        if self.state.sel_dest is None:
            self.toast_overlay.add_toast(Adw.Toast(
                title="Select a station first", timeout=3))
            return
        self._busy_tx = True
        self.send_btn.set_sensitive(False)
        self.state.seq += 1
        args = dict(text=text, source_id=self.state.my_id,
                    dest_id=self.state.sel_dest, profile=self.state.current_profile,
                    snr_db=self.state.current_snr, channel=self.state.channel,
                    fec=self.state.fec, seq=self.state.seq)
        self._log(f">> {self.state.callsign}: {text}")
        wink_engine.run_async(lambda: wink_engine.send_text(**args), self._on_sent)

    def _on_sent(self, res):
        self._busy_tx = False
        self.send_btn.set_sensitive(True)
        if not res.get("ok"):
            self._log(f"-- TX error: {res.get('error', '?')} --")
            return
        if res["decoded"]:
            self._log(f"<< 0x{res['source']:08x}: {res['text']}")
            self.state.record_decode(res["text"], res["snr_db"])
            if res["recommended"] != res["profile"]:
                self._log(f"-- link suggests {res['recommended']} "
                          f"(now {res['profile']}) --")
        else:
            self._log(f"-- no decode @ {res['snr_db']:.0f} dB "
                      f"({res['profile']}/{res['channel']}/{res['fec']}) --")
            self.state.record_miss(res["snr_db"])
class StationPage(Gtk.Box):
    """Station identity (callsign/locator/power) and firmware
    connection -- operational config you change per-session. App-wide
    preferences (audio devices, etc.) live in the real Preferences
    dialog (menu \u2192 Preferences), not here -- those are two different
    kinds of "settings" and conflating them into one tab was the
    original mistake."""

    def __init__(self, state: AppState, toast_overlay: Adw.ToastOverlay):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.state = state
        self.toast_overlay = toast_overlay

        station = Adw.PreferencesGroup(title="Station", description="Mirrors beacon/config.h")
        self.callsign_row = Adw.EntryRow(title="Callsign")
        self.callsign_row.set_text("VK3LOG")
        self.locator_row = Adw.EntryRow(title="Locator (4-char)")
        self.locator_row.set_text("QF22")
        self.power_row = Adw.SpinRow.new_with_range(0, 63, 1)
        self.power_row.set_title("Power (dBm)")
        self.power_row.set_value(27)
        station.add(self.callsign_row)
        station.add(self.locator_row)
        station.add(self.power_row)
        self.dial_row = Adw.SpinRow.new_with_range(1000000, 60000000, 100)
        self.dial_row.set_title("Dial frequency (Hz)")
        self.dial_row.set_subtitle("Wideband offsets add to this for reply QSY")
        self.dial_row.set_value(state.dial_freq_hz)
        station.add(self.dial_row)
        self.append(station)

        save_btn = Gtk.Button(label="Save", css_classes=["suggested-action"], halign=Gtk.Align.END)
        save_btn.connect("clicked", self._on_save)
        self.append(save_btn)

        conn = Adw.PreferencesGroup(
            title="Rig (Hamlib rigctld)",
            description="Pick a radio and serial port, launch rigctld, then "
                        "Connect -- the WSJT-X Radio-tab flow. Or start the "
                        "in-process fake rig for a hardware-free demo.",
        )
        self.rig_model_row = Adw.ComboRow(title="Rig model")
        self._rig_names = [name for name, _ in wink_rig.RIG_MODELS] + ["Manual model number\u2026"]
        self.rig_model_row.set_model(Gtk.StringList.new(self._rig_names))
        self.rig_model_row.set_selected(2)  # IC-7300, the common case
        conn.add(self.rig_model_row)
        self.rig_model_num = Adw.SpinRow.new_with_range(1, 99999, 1)
        self.rig_model_num.set_title("Model number override")
        self.rig_model_num.set_subtitle("Only used with \u201cManual model number\u2026\u201d "
                                        "(`rigctl -l` is authoritative)")
        self.rig_model_num.set_value(3073)
        conn.add(self.rig_model_num)
        self.serial_row = Adw.EntryRow(title="Serial port")
        self.serial_row.set_text("/dev/ttyUSB0")
        conn.add(self.serial_row)
        self.baud_row = Adw.ComboRow(title="Baud rate")
        self._bauds = [str(b) for b in wink_rig.COMMON_BAUDS]
        self.baud_row.set_model(Gtk.StringList.new(self._bauds))
        self.baud_row.set_selected(self._bauds.index("9600"))
        conn.add(self.baud_row)
        self.host_row = Adw.EntryRow(title="rigctld host")
        self.host_row.set_text(state.rig_host)
        self.port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port_row.set_title("rigctld port")
        self.port_row.set_value(state.rig_port)
        conn.add(self.host_row)
        conn.add(self.port_row)
        self.append(conn)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.END)
        fake_btn = Gtk.Button(label="Start fake rig")
        fake_btn.connect("clicked", self._on_fake_rig)
        btns.append(fake_btn)
        self.launch_btn = Gtk.Button(label="Launch rigctld")
        self.launch_btn.connect("clicked", self._on_launch)
        btns.append(self.launch_btn)
        self.conn_btn = Gtk.Button(label="Connect", css_classes=["suggested-action"])
        self.conn_btn.connect("clicked", self._on_connect)
        btns.append(self.conn_btn)
        self.append(btns)

        self.rig_status = Adw.PreferencesGroup(title="Rig status")
        self.rig_status_row = Adw.ActionRow(title="Not connected")
        self.rig_status.add(self.rig_status_row)
        self.append(self.rig_status)
        state.subscribe(self._refresh_rig_status)
        self._refresh_rig_status()

    def _on_save(self, *_):
        self.state.callsign = self.callsign_row.get_text().strip() or self.state.callsign
        self.state.locator = self.locator_row.get_text().strip() or self.state.locator
        self.state.dial_freq_hz = int(self.dial_row.get_value())
        self.state.notify()
        self.toast_overlay.add_toast(Adw.Toast(title="Settings saved (local only)", timeout=2))

    def _selected_model(self) -> int:
        idx = self.rig_model_row.get_selected()
        if idx >= len(wink_rig.RIG_MODELS):
            return int(self.rig_model_num.get_value())
        return wink_rig.RIG_MODELS[idx][1]

    def _on_launch(self, *_):
        model = self._selected_model()
        serial = self.serial_row.get_text().strip()
        baud = int(self._bauds[self.baud_row.get_selected()])
        self.launch_btn.set_sensitive(False)
        wink_engine.run_async(
            lambda: self._do_launch(model, serial, baud),
            self._on_launch_done)

    def _do_launch(self, model, serial, baud):
        try:
            if self.state._rigctld_proc is not None:
                try:
                    self.state._rigctld_proc.terminate()
                except Exception:
                    pass
            proc = wink_rig.launch_rigctld(model, serial, baud)
            self.state._rigctld_proc = proc
            self.state.rig_host = "127.0.0.1"
            self.state.rig_port = wink_rig.DEFAULT_PORT
            return {"ok": True, "msg": f"rigctld running (model {model}) -- press Connect"}
        except wink_rig.RigError as exc:
            return {"ok": False, "msg": str(exc)}

    def _on_launch_done(self, res):
        self.launch_btn.set_sensitive(True)
        self.host_row.set_text(self.state.rig_host)
        self.port_row.set_value(self.state.rig_port)
        self.toast_overlay.add_toast(Adw.Toast(title=res["msg"], timeout=6))

    def _on_fake_rig(self, *_):
        port = self.state.start_fake_rig()
        self.port_row.set_value(port)
        self.toast_overlay.add_toast(Adw.Toast(
            title=f"Fake rig listening on 127.0.0.1:{port} -- press Connect", timeout=4))

    def _on_connect(self, *_):
        if self.state.rig_connected:
            self.state.rig_disconnect()
            self.conn_btn.set_label("Connect")
            return
        self.state.rig_host = self.host_row.get_text().strip() or "127.0.0.1"
        self.state.rig_port = int(self.port_row.get_value())
        self.conn_btn.set_sensitive(False)
        wink_engine.run_async(
            self.state.rig_connect,
            self._on_connect_done)

    def _on_connect_done(self, res):
        ok, msg = res
        self.conn_btn.set_sensitive(True)
        self.conn_btn.set_label("Disconnect" if ok else "Connect")
        self.toast_overlay.add_toast(Adw.Toast(title=msg, timeout=5))

    def _refresh_rig_status(self):
        if self.state.rig_connected:
            self.rig_status_row.set_title("Connected")
            self.rig_status_row.set_subtitle(
                f"{self.state.rig_freq_hz} Hz \u00b7 {self.state.rig_mode} \u00b7 "
                f"{self.state.rig_host}:{self.state.rig_port}")
            self.conn_btn.set_label("Disconnect")
        else:
            self.rig_status_row.set_title("Not connected")
            self.rig_status_row.set_subtitle("Connect a rigctld above (or start the fake rig)")
            self.conn_btn.set_label("Connect")


class AudioPreferencesWindow(Adw.PreferencesWindow):
    """Real sound-card selection, backed by audio_devices.py
    (sounddevice/PortAudio) -- not a static mock list. Handles the
    "PortAudio not installed" case explicitly rather than pretending
    devices exist, and Test actually asks the device whether it
    supports 12kHz mono (what WINK needs) rather than just confirming
    it's present in a list."""

    def __init__(self, state: AppState, toast_overlay: Adw.ToastOverlay, **kwargs):
        super().__init__(title="Preferences", **kwargs)
        self.state = state
        self.toast_overlay = toast_overlay
        self._input_devices = []
        self._output_devices = []

        page = Adw.PreferencesPage(title="Audio", icon_name="audio-card-symbolic")
        self.add(page)

        if not audio_devices.AVAILABLE:
            group = Adw.PreferencesGroup(title="Audio devices")
            group.add(Adw.ActionRow(
                title="Audio backend unavailable",
                subtitle=audio_devices.UNAVAILABLE_REASON,
            ))
            page.add(group)
            return

        group = Adw.PreferencesGroup(
            title="Audio devices",
            description="For the modem's TX/RX audio path once firmware/real hardware is connected.",
        )

        self.input_row = Adw.ComboRow(title="Input (RX)")
        self.output_row = Adw.ComboRow(title="Output (TX)")
        group.add(self.input_row)
        group.add(self.output_row)

        test_row = Adw.ActionRow(title="Test selected devices")
        test_btn = Gtk.Button(label="Test", valign=Gtk.Align.CENTER, css_classes=["flat"])
        test_btn.connect("clicked", self._on_test)
        test_row.add_suffix(test_btn)
        group.add(test_row)

        self.level_row = Adw.ActionRow(title="Input level")
        self.level_bar = Gtk.LevelBar(min_value=0.0, max_value=1.0,
                                      valign=Gtk.Align.CENTER)
        self.level_bar.set_size_request(160, -1)
        self.level_row.add_suffix(self.level_bar)
        group.add(self.level_row)

        refresh_row = Adw.ActionRow(
            title="Refresh device list",
            subtitle="Devices can appear/disappear (e.g. plugging in a USB sound card)",
        )
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        refresh_btn.connect("clicked", lambda *_: self._populate())
        refresh_row.add_suffix(refresh_btn)
        group.add(refresh_row)

        page.add(group)

        self.input_row.connect("notify::selected", self._on_input_changed)
        self.output_row.connect("notify::selected", self._on_output_changed)
        self._populate()

    def _populate(self):
        self._input_devices = audio_devices.list_input_devices()
        self._output_devices = audio_devices.list_output_devices()

        in_names = [d["name"] for d in self._input_devices] or ["(no input devices found)"]
        out_names = [d["name"] for d in self._output_devices] or ["(no output devices found)"]
        self.input_row.set_model(Gtk.StringList.new(in_names))
        self.output_row.set_model(Gtk.StringList.new(out_names))
        self.input_row.set_sensitive(bool(self._input_devices))
        self.output_row.set_sensitive(bool(self._output_devices))

        default_in = audio_devices.default_input_index()
        default_out = audio_devices.default_output_index()
        for i, d in enumerate(self._input_devices):
            if d["index"] == default_in:
                self.input_row.set_selected(i)
        for i, d in enumerate(self._output_devices):
            if d["index"] == default_out:
                self.output_row.set_selected(i)



    def _on_input_changed(self, row, _pspec):
        if self._input_devices:
            idx = row.get_selected()
            if idx < len(self._input_devices):
                self.state.audio_input_name = self._input_devices[idx]["name"]
                self.state.notify()

    def _on_output_changed(self, row, _pspec):
        if self._output_devices:
            idx = row.get_selected()
            if idx < len(self._output_devices):
                self.state.audio_output_name = self._output_devices[idx]["name"]
                self.state.notify()

    def _on_test(self, *_):
        results = []
        if self._input_devices:
            sel = self.input_row.get_selected()
            if sel < len(self._input_devices):
                global_idx = self._input_devices[sel]["index"]
                ok, msg = audio_devices.test_input_device(global_idx)
                results.append(f"Input: {'OK' if ok else 'FAILED'} \u2014 {msg}")
                if ok:
                    self._update_level(global_idx)
        if self._output_devices:
            sel = self.output_row.get_selected()
            if sel < len(self._output_devices):
                global_idx = self._output_devices[sel]["index"]
                ok, msg = audio_devices.test_output_device(global_idx)
                results.append(f"Output: {'OK' if ok else 'FAILED'} \u2014 {msg}")
        if not results:
            results = ["No devices to test"]
        self.toast_overlay.add_toast(Adw.Toast(title=" / ".join(results), timeout=4))

    def _update_level(self, global_idx):
        # Half-second blocking read in a thread; WSJT-X's green bar equivalent.
        import threading
        self.level_row.set_subtitle("measuring\u2026")
        threading.Thread(target=self._level_worker, args=(global_idx,),
                         daemon=True).start()

    def _level_worker(self, global_idx):
        ok, peak, msg = audio_devices.input_level(global_idx)
        GLib.idle_add(self._on_level, ok, peak, msg)

    def _on_level(self, ok, peak, msg):
        if ok:
            self.level_bar.set_value(peak)
            hint = "aim ~0.3-0.7 (WSJT-X green zone)" if peak < 0.95 \
                else "CLIPPING -- turn the input down"
            self.level_row.set_subtitle(f"peak {peak:.2f} of full scale -- {hint}")
        else:
            self.level_bar.set_value(0.0)
            self.level_row.set_subtitle(f"level read failed: {msg}")
        return False


class WinkMonitorWindow(Adw.ApplicationWindow):
    """One main page (stations + RX/TX) like WSJT-X; settings live
    behind the hamburger menu, not in tabs."""

    def __init__(self, app):
        super().__init__(application=app, title="WINK Monitor",
                         default_width=1020, default_height=680)

        self.state = state = AppState()
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="WINK Monitor"))
        toolbar_view.add_top_bar(header)

        self.toast_overlay = Adw.ToastOverlay()

        menu = Gio.Menu()
        menu.append("Station & Rig\u2026", "win.station")
        menu.append("Audio\u2026", "win.preferences")
        menu.append("About", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        for name, cb in (("station", self._on_station),
                         ("preferences", self._on_preferences),
                         ("about", self._on_about)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

        self.qso = QsoPage(state, self.toast_overlay)
        self.toast_overlay.set_child(self.qso)
        toolbar_view.set_content(self.toast_overlay)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
                         margin_top=4, margin_bottom=4, margin_start=12, margin_end=12)
        self.rig_status_lbl = Gtk.Label(label="rig: not connected", halign=Gtk.Align.START)
        self.decode_status_lbl = Gtk.Label(label="engine idle", halign=Gtk.Align.START, hexpand=True)
        self.profile_status_lbl = Gtk.Label(label="", halign=Gtk.Align.END)
        status.append(self.rig_status_lbl)
        status.append(self.decode_status_lbl)
        status.append(self.profile_status_lbl)
        toolbar_view.add_bottom_bar(status)
        state.subscribe(self._refresh_status)
        self._refresh_status()

        self.set_content(toolbar_view)

    def _refresh_status(self):
        if self.state.rig_connected:
            self.rig_status_lbl.set_label(
                f"rig: {self.state.rig_freq_hz} Hz {self.state.rig_mode}")
        else:
            self.rig_status_lbl.set_label("rig: not connected")
        if self.state.last_rx_text is None and self.state.last_decode_ok is False:
            self.decode_status_lbl.set_label("last TX: no decode")
        elif self.state.last_rx_text is None:
            self.decode_status_lbl.set_label("engine idle -- scan, select, send")
        else:
            self.decode_status_lbl.set_label(
                f"last RX @ {self.state.current_snr:.0f} dB")
        self.profile_status_lbl.set_label(
            f"{self.state.current_profile} \u00b7 {self.state.channel}/{self.state.fec}")

    def _on_station(self, *_):
        # Reuse the StationPage box inside a plain window page.
        box = StationPage(self.state, self.toast_overlay)
        win = Adw.Window(title="Station & Rig", transient_for=self, modal=True,
                         default_width=560, default_height=640)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(box)
        win.set_content(scroll)
        win.present()

    def _on_preferences(self, *_):
        prefs = AudioPreferencesWindow(self.state, self.toast_overlay, transient_for=self, modal=True)
        prefs.present()

    def _on_about(self, *_):
        Adw.AboutWindow(transient_for=self, application_name="WINK Monitor",
                        version="0.2", application_icon="radio-symbolic",
                        developer_name="VK3LOG",
                        comments="Keyboard-to-keyboard modem UI over the validated WINK stack. "
                                 "Channel is simulated; misses are real decoder failures.").present()


class WinkMonitorApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="net.vk3log.winkmonitor")

    def do_activate(self):
        win = self.props.active_window or WinkMonitorWindow(self)
        win.present()


if __name__ == "__main__":
    WinkMonitorApp().run()
