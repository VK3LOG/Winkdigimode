#!/usr/bin/env python3
"""WINKMESH flood-relay demo: A -> B -> C, where A and C can't hear
each other directly but B has opted in as a flood relay.

Each hop goes through the real channel model (simulate_packet), not
just the relay bookkeeping -- so this proves the TTL/dedup logic AND
the physical layer work together, the same "build it, then measure it"
standard as the beacon/waveform work.
"""
import numpy as np
from wink_core import Packet, callsign_id
from wink_modem2 import simulate_packet
from wink_profiles import PROFILES, DEFAULT_FEC, DEFAULT_LLR_MODE
from wink_mesh import MeshNode, BROADCAST_DEST

PROFILE = "WINK-N"


def hop(pkt, snr_db, seed):
    c = PROFILES[PROFILE]
    rng = np.random.default_rng(seed)
    front_pad = int(rng.integers(0, c.sps))
    cfo = float(rng.uniform(-3, 3))
    return simulate_packet(c, pkt, snr_db, rng, cfo, front_pad,
                            fec=DEFAULT_FEC, llr_mode=DEFAULT_LLR_MODE)


def run():
    A, B, C = callsign_id("VK3AAA"), callsign_id("VK3BBB"), callsign_id("VK3CCC")

    # A<->C link is too weak to close direct (-30dB, below WINK-N's
    # threshold); A<->B and B<->C are both solid (-10dB).
    LINK_AB, LINK_BC, LINK_AC = -10.0, -10.0, -30.0

    node_a = MeshNode(A, flood_relay=False)
    node_b = MeshNode(B, flood_relay=True)   # opted in
    node_c = MeshNode(C, flood_relay=False)

    msg = Packet(kind=1, source=A, dest=C, seq=1, payload=b"hi C, via mesh")

    print("--- Attempt 1: direct A -> C (should fail, link too weak) ---")
    direct = hop(msg, LINK_AC, seed=1)
    print("direct decode:", direct.payload if direct else None)

    print("\n--- Attempt 2: A -> B -> C via opt-in flood relay ---")
    # Hop 1: A transmits, B receives.
    rx_at_b = hop(msg, LINK_AB, seed=2)
    print("B receives:", rx_at_b.payload if rx_at_b else None)
    if rx_at_b is None:
        return
    delivered_b, relay_pkt = node_b.receive(rx_at_b)
    print(f"B: delivered_to_app={delivered_b} (B is not the dest, correct) "
          f"relaying={relay_pkt is not None} ttl_after={relay_pkt.ttl if relay_pkt else None}")

    # Hop 2: B relays, C receives.
    rx_at_c = hop(relay_pkt, LINK_BC, seed=3)
    print("C receives:", rx_at_c.payload if rx_at_c else None)
    delivered_c, relay_pkt2 = node_c.receive(rx_at_c)
    print(f"C: delivered_to_app={delivered_c} (C is the dest, correct) "
          f"relaying={relay_pkt2 is not None} (C didn't opt in, correct)")

    print("\n--- Attempt 3: dedup -- B re-hears its own relay somehow, must not re-relay ---")
    delivered_dup, relay_dup = node_b.receive(relay_pkt)
    print(f"B re-processing same (source,seq): delivered={delivered_dup} "
          f"relay_again={relay_dup is not None} (must be False -- dedup working)")

    print("\n--- Attempt 4: TTL exhaustion -- relay with ttl=0 must not propagate further ---")
    exhausted = Packet(kind=1, source=A, dest=C, seq=99, payload=b"dead packet", ttl=0)
    _, relay_none = node_b.receive(exhausted)
    print(f"relay attempted with ttl=0: {relay_none is not None} (must be False)")


def flood(topology: dict, links: dict, origin: int, pkt: Packet, seed_base: int):
    """Discrete-event flood over a topology through the real channel.

    topology: node_id -> MeshNode. links: (tx, rx) -> snr_db for pairs
    that can hear each other. Returns (deliveries, transmissions).
    Every transmission goes through simulate_packet; a failed decode
    simply doesn't arrive (honest PHY + logic integration).
    """
    delivered = []
    tx_count = 0
    # (packet, transmitting_node); origin transmits first
    queue = [(pkt, origin)]
    seed = seed_base
    guard = 0
    while queue and guard < 64:
        guard += 1
        cur, txnode = queue.pop(0)
        for (tx, rx), snr in links.items():
            if tx != txnode:
                continue
            seed += 1
            heard = hop(cur, snr, seed)
            tx_count += 1
            if heard is None:
                continue
            node = topology[rx]
            got_app, relay = node.receive(heard)
            if got_app:
                delivered.append(rx)
            if relay is not None:
                queue.append((relay, rx))
    return delivered, tx_count


def run_topologies():
    ids = {n: callsign_id(f"VK3{n}") for n in ("AAA", "BBB", "CCC", "DDD", "EEE")}
    A, B, C, D, E = (ids[k] for k in ("AAA", "BBB", "CCC", "DDD", "EEE"))

    print("\n=== Topology 1: 5-node line A-B-C-D-E (relays B,C,D) ===")
    topo = {A: MeshNode(A), B: MeshNode(B, True), C: MeshNode(C, True),
            D: MeshNode(D, True), E: MeshNode(E)}
    links = {(A, B): -10, (B, A): -10, (B, C): -10, (C, B): -10,
             (C, D): -10, (D, C): -10, (D, E): -10, (E, D): -10}
    msg = Packet(1, A, E, 7, b"down the line", ttl=4)
    got, ntx = flood(topo, links, A, msg, 100)
    print(f"E delivered: {E in got} (must be True)  transmissions: {ntx}")
    assert E in got

    print("\n=== Topology 2: diamond A->B->D + A->C->D (two paths) ===")
    topo = {A: MeshNode(A), B: MeshNode(B, True), C: MeshNode(C, True),
            D: MeshNode(D)}
    links = {(A, B): -10, (A, C): -10, (B, D): -10, (C, D): -10,
             (B, A): -10, (C, A): -10, (D, B): -10, (D, C): -10}
    msg = Packet(1, A, D, 8, b"via diamond", ttl=4)
    got, ntx = flood(topo, links, A, msg, 200)
    print(f"D deliveries: {got.count(D)} (must be exactly 1 -- dedup across paths)")
    assert got.count(D) == 1

    print("\n=== Topology 3: broadcast from A, relays B,C,D ===")
    topo = {A: MeshNode(A), B: MeshNode(B, True), C: MeshNode(C, True),
            D: MeshNode(D, True), E: MeshNode(E)}
    links = {(A, B): -10, (B, A): -10, (B, C): -10, (C, B): -10,
             (C, D): -10, (D, C): -10, (D, E): -10, (E, D): -10,
             (B, D): -30, (D, B): -30}
    msg = Packet(1, A, BROADCAST_DEST, 9, b"all stations", ttl=4)
    got, ntx = flood(topo, links, A, msg, 300)
    heard = sorted(set(got))
    print(f"nodes hearing broadcast: {len(heard)} (A,B,C,D,E = 5, must all hear)")
    assert set(heard) == {A, B, C, D, E}, heard


if __name__ == "__main__":
    run()
    run_topologies()
    print("\nALL MESH TOPOLOGY TESTS PASS")
