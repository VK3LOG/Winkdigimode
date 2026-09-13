"""WINKMESH -- opt-in flood relay.

Design per WINKMESH_DESIGN_NOTES.md: flooding is simple and needs no
routing tables, but repeating the *entire* channel-time cost of a
packet at every hop doesn't scale if every station does it. So relaying
is off by default; a station (or a dedicated beacon) has to explicitly
opt in via MeshNode(flood_relay=True).

Loop prevention: TTL (decrements each hop, dropped at 0) + a
seen-packet dedup cache keyed on (source, seq), both required together
-- TTL alone still lets duplicate copies circulate on any topology with
more than one path between two relays until they all expire.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from wink_core import Packet

BROADCAST_DEST = 0xFFFFFFFF


@dataclass
class MeshNode:
    node_id: int
    flood_relay: bool = False       # opt-in: does this station rebroadcast
                                     # other stations' traffic?
    seen_cache_size: int = 256
    _seen: set = field(default_factory=set, repr=False)

    def _mark_seen(self, pkt: Packet) -> bool:
        """Returns True if this is a new packet (not a dup)."""
        key = (pkt.source, pkt.seq)
        if key in self._seen:
            return False
        self._seen.add(key)
        if len(self._seen) > self.seen_cache_size:
            self._seen.pop()  # arbitrary eviction; fine for a dedup cache
        return True

    def receive(self, pkt: Packet) -> tuple[bool, Packet | None]:
        """Process an incoming packet at this node.

        Returns (delivered_to_app, packet_to_relay_or_None).
        delivered_to_app is True if this node is the addressee (or the
        packet is a broadcast) and the app layer should see it.
        packet_to_relay_or_None is a decremented-TTL copy to retransmit,
        if this node is an opted-in relay and the packet still has hops
        left -- caller is responsible for actually transmitting it.
        """
        is_new = self._mark_seen(pkt)
        # Dedup applies to delivery too: the app must not see the same
        # (source, seq) twice when two paths deliver it (diamond topology).
        delivered = is_new and pkt.dest in (self.node_id, BROADCAST_DEST)

        relay_copy = None
        if is_new and self.flood_relay and pkt.ttl > 0:
            # Don't bother relaying something already addressed to us
            # and not a broadcast -- nothing further to reach.
            if pkt.dest == BROADCAST_DEST or pkt.dest != self.node_id:
                relay_copy = Packet(pkt.kind, pkt.source, pkt.dest, pkt.seq,
                                     pkt.payload, ttl=pkt.ttl - 1)

        return delivered, relay_copy
