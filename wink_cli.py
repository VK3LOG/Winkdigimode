"""WINK v0.3 CLI — self-contained portable executable entry point.

Bundled by PyInstaller with --paths pointing at the v02 directory, so all
WINK imports resolve statically at build time (no runtime sys.path hacks,
which is what broke the earlier onefile build).

Portability contract:
  * Nothing is read from the build machine after bundling.
  * The only runtime requirement is a writable temp dir (the onefile
    bootloader extracts itself there) and a place to write outputs.
  * `wink setup` provisions that output location on first run and is
    idempotent, so the binary works on a fresh machine with no extra
    files copied alongside it.
"""
import argparse
import json
import os
import sys
import time

import numpy as np

from wink_core import FEC_CONFIGS, Packet, callsign_id
from wink_modem2 import simulate_packet
from wink_profiles import (
    DEFAULT_FEC,
    DEFAULT_LLR_MODE,
    ORDER,
    PROFILES,
    STRONG_FEC,
    THRESHOLDS,
    THRESHOLDS_STRONG,
    recommend_profile,
)


def workdir_from_args(parsed) -> str:
    """Resolve the output/work directory: env > flag > ./wink_out."""
    override = os.environ.get("WINK_WORKDIR") or getattr(parsed, "outdir", None)
    return override or os.path.join(os.path.curdir, "wink_out")


def ensure_workdir(path: str) -> dict:
    """Create the whole writable file tree the EXE uses. Idempotent."""
    tree = {
        "results": os.path.join(path, "results"),
        "config": os.path.join(path, "config"),
        "tmp": os.path.join(path, "tmp"),
    }
    for sub in (path, *tree.values()):
        os.makedirs(sub, exist_ok=True)
    return tree


def snap_state() -> dict:
    return {
        "profiles": {
            name: {"baud": PROFILES[name].baud, "spacing": PROFILES[name].spacing,
                   "M": PROFILES[name].M,
                   "threshold_k9_soft_db": THRESHOLDS[name],
                   "threshold_k9_1_3_db": THRESHOLDS_STRONG[name]}
            for name in ORDER
        },
        "fec": {name: {"K": c["K"], "rate": f"1/{len(c['Gs'])}",
                       "generators": [oct(g) for g in c["Gs"]]}
                for name, c in FEC_CONFIGS.items()},
        "default_fec": DEFAULT_FEC,
        "strong_fec": STRONG_FEC,
        "llr_mode": DEFAULT_LLR_MODE,
    }


def cmd_list(_a):
    print("WINK profiles (order for adaptive switching: S -> N -> F)")
    print(f"{'':-<62}")
    for name in ORDER:
        c = PROFILES[name]
        print(f"{name:6} M={c.M} baud={c.baud:3.0f} spacing={c.spacing:4.1f} Hz"
              f"  K9/soft 50%: {THRESHOLDS[name]:6.1f} dB"
              f"  K9_1_3 50%: {THRESHOLDS_STRONG[name]:6.1f} dB")
    print(f"default FEC: {DEFAULT_FEC}  strong FEC: {STRONG_FEC}  LLR: {DEFAULT_LLR_MODE}")
    for name, cfg in FEC_CONFIGS.items():
        print(f"  {name:9} K={cfg['K']} rate=1/{len(cfg['Gs'])} Gs={[oct(g) for g in cfg['Gs']]}")


def cmd_demo(args):
    import demo_adaptive_qso

    sys.argv = [sys.argv[0]] + (["--ultra"] if args.ultra else [])
    demo_adaptive_qso.run()


def cmd_beacon(_args):
    import wink_beacon

    raw = wink_beacon.encode_beacon("VK3LOG", "QF22", 27)
    dec = wink_beacon.decode_beacon(raw)
    print(f"beacon encode -> {raw.hex()}  decode -> {dec}")
    for fec in (DEFAULT_FEC, STRONG_FEC):
        t0 = time.time()
        ok = 0
        for t in range(10):
            r = wink_beacon.simulate_beacon(
                "VK3LOG", "QF22", 27, -30.0, np.random.default_rng(4000 + t),
                fec=fec, llr_mode="soft")
            ok += int(r is not None and r[0] == "VK3LOG")
        print(f"beacon {fec} @ -30 dB: {ok}/10   ({time.time()-t0:.0f}s)")


def cmd_fec(args):
    c = PROFILES["WINK-S"]
    pkt = Packet(1, callsign_id("VK3TEST"), callsign_id("VK5TEST"), 1, args.payload.encode())
    rows = [["fec", "snr_db", "successful", "trials", "success_rate"]]
    print(f"{'fec':8} {'snr':>5}  {'ok':>4} rate")
    for fec in (DEFAULT_FEC, STRONG_FEC):
        for snr in args.snr:
            good = 0
            for t in range(args.trials):
                rng = np.random.default_rng(hash((fec, snr, t)) & 0xFFFFFFFF)
                rx = simulate_packet(c, pkt, snr, rng, float(rng.uniform(-3, 3)),
                                     int(rng.integers(0, c.sps)), fec=fec, llr_mode="soft")
                good += int(rx is not None and rx.payload == pkt.payload)
            rows.append([fec, snr, good, args.trials, good / args.trials])
            print(f"{fec:8} {snr:5.1f}  {good:3d}/{args.trials}")
    tree = ensure_workdir(workdir_from_args(args))
    out = os.path.join(tree["results"], "fec_winks.csv")
    with open(out, "w", newline="") as fh:
        import csv

        csv.writer(fh).writerows(rows)
    print(f"results written -> {out}")


def cmd_next(args):
    print("next profile:", recommend_profile(args.snr_db, args.current))


def cmd_setup(args):
    path = workdir_from_args(args)
    tree = ensure_workdir(path)
    state = snap_state()
    state_path = os.path.join(tree["config"], "wink_state.json")
    with open(state_path, "w") as fh:
        json.dump(state, fh, indent=2)
    print(f"created workdir: {path}")
    for sub in tree:
        print(f"  {sub:8} -> {tree[sub]}")
    print(f"state written -> {state_path}")
    print("(idempotent - rerunning just refreshes the state file)")


def cmd_rig(args):
    import wink_rig
    if args.rig_cmd == "probe":
        ok = wink_rig.probe(args.host, args.port)
        print(f"rigctld at {args.host}:{args.port}: {'answering' if ok else 'not found'}")
        if not ok:
            print("start one: rigctld -m <model> -r <serial-device>  (`rigctl -l` lists models)")
        return
    try:
        with wink_rig.RigClient(args.host, args.port) as rig:
            if args.rig_cmd == "freq":
                if args.set is not None:
                    rig.set_freq(int(args.set))
                print(f"freq: {rig.get_freq()} Hz")
            elif args.rig_cmd == "mode":
                mode, pb = rig.get_mode()
                print(f"mode: {mode} passband {pb} Hz")
            elif args.rig_cmd == "ptt":
                if args.state is not None:
                    rig.set_ptt(args.state.lower() in ("1", "on", "true"))
                print(f"ptt: {'ON' if rig.get_ptt() else 'OFF'}")
            elif args.rig_cmd == "smeter":
                print(f"s-meter (rig units 0..1): {rig.get_strength():.2f}")
    except wink_rig.RigError as exc:
        print(f"rig error: {exc}")


def cmd_scan(args):
    import wink_engine
    t0 = time.time()
    res = wink_engine.wideband_trial(args.snr, args.profile, fec=args.fec)
    if not res.get("ok"):
        print(f"scan failed: {res.get('error', '?')}")
        return
    if not res["stations"]:
        print(f"nothing heard @ {args.snr} dB ({res['elapsed_s']:.0f}s)")
        return
    for st in res["stations"]:
        try:
            payload = st["payload"].decode(errors="replace")
        except Exception:
            payload = repr(st["payload"])
        print(f"0x{st['source']:08x} seq={st['seq']} offset={st['offset_hz']:+.1f}Hz "
              f"cfo={st.get('cfo_hz', 0.0):+.1f}Hz conf={st['confidence']:.0%} "
              f"\u201c{payload}\u201d")
    print(f"({res['elapsed_s']:.0f}s scan)")


def cmd_send(args):
    import wink_engine
    from wink_core import callsign_id as _ci
    try:
        dest = int(args.to, 0)
    except ValueError:
        try:
            dest = _ci(args.to)
        except ValueError as exc:
            print(f"bad destination: {exc}")
            return
    t0 = time.time()
    res = wink_engine.send_text(args.text, _ci(args.me), dest, args.profile,
                                args.snr, channel=args.channel, fec=args.fec,
                                seq=args.seq)
    if not res.get("ok"):
        print(f"TX error: {res.get('error', '?')}")
        return
    if res["decoded"]:
        print(f"<< 0x{res['source']:08x}: {res['text']}  ({time.time()-t0:.0f}s)")
        print(f"link suggests {res['recommended']}")
    else:
        print(f"-- no decode @ {args.snr} dB "
              f"({args.profile}/{args.channel}/{args.fec}) --")


def main():
    p = argparse.ArgumentParser(description="WINK digital-mode CLI (portable)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def wd(sp):
        sp.add_argument("--outdir", default=None,
                        help="work dir for outputs (default: ./wink_out, or $WINK_WORKDIR)")

    sub.add_parser("list", help="show profiles, thresholds, FEC configs")
    d = sub.add_parser("demo", help="run the adaptive S/N/F QSO demo")
    d.add_argument("--ultra", action="store_true", help="use rate-1/3 K9_1_3 FEC")
    wd(d)
    b = sub.add_parser("beacon", help="encode/simulate the WINK beacon")
    wd(b)
    f = sub.add_parser("fec", help="quick K9 vs K9_1_3 comparison on WINK-S")
    f.add_argument("snr", nargs="*", type=float, default=[-24.0, -25.0, -26.0])
    f.add_argument("--trials", type=int, default=20)
    f.add_argument("--payload", default="hello WINK")
    wd(f)
    n = sub.add_parser("next", help="what profile does recommend_profile choose?")
    n.add_argument("snr_db", type=float)
    n.add_argument("current", choices=list(PROFILES.keys()))
    s = sub.add_parser("setup", help="create the workdir files the EXE needs (idempotent)")
    wd(s)
    r = sub.add_parser("rig", help="talk to a Hamlib rigctld (freq/mode/ptt/s-meter)")
    r.add_argument("rig_cmd", choices=("probe", "freq", "mode", "ptt", "smeter"))
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=4532)
    r.add_argument("--set", default=None, help="freq: set frequency Hz")
    r.add_argument("--state", default=None, help="ptt: on|off")
    sc = sub.add_parser("scan", help="real wideband scan of a simulated passband")
    sc.add_argument("--snr", type=float, default=-10.0)
    sc.add_argument("--profile", choices=list(PROFILES.keys()), default="WINK-S")
    sc.add_argument("--fec", choices=("K7", "K9", "K9_1_3"), default=DEFAULT_FEC)
    sd = sub.add_parser("send", help="send one TEXT packet through the real channel")
    sd.add_argument("text")
    sd.add_argument("--to", default="0xFFFFFFFF", help="dest id or callsign (default: CQ broadcast)")
    sd.add_argument("--me", default="VK3LOG")
    sd.add_argument("--profile", choices=list(PROFILES.keys()), default="WINK-N")
    sd.add_argument("--snr", type=float, default=-14.0)
    sd.add_argument("--channel", choices=("awgn", "fading"), default="awgn")
    sd.add_argument("--fec", choices=("K7", "K9", "K9_1_3"), default=DEFAULT_FEC)
    sd.add_argument("--seq", type=int, default=1)
    args = p.parse_args()
    {"list": cmd_list, "demo": cmd_demo, "beacon": cmd_beacon,
     "fec": cmd_fec, "next": cmd_next, "setup": cmd_setup,
     "rig": cmd_rig, "scan": cmd_scan, "send": cmd_send}[args.cmd](args)


if __name__ == "__main__":
    main()