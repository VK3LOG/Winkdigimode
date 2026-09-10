# WINKMESH — opt-in flood relay (implemented, validated)

**Decision made and built:** flooding, opt-in per station. Off by
default; a station (or a dedicated beacon) explicitly sets
`MeshNode(flood_relay=True)` to participate. This sidesteps flooding's
usual scaling problem (every hop repeating the full channel-time cost)
by construction — most stations simply don't relay, so it only costs
channel time on the links that chose to spend it.

## What's built (`wink_mesh.py`, `demo_mesh_relay.py`)

- `Packet` gained a `ttl` field (repurposed the previously-unused
  reserved header byte — same wire position, not a breaking format
  change; old code always sent 0 there anyway).
- `MeshNode.receive()`: TTL decrement + a `(source, seq)` dedup cache,
  both required together — TTL alone still lets duplicates circulate
  on any topology with more than one path between relays.
- Validated end-to-end through the **real physical-layer channel
  model** (not just the relay bookkeeping in isolation): a 3-node
  A→B→C chain where A and C can't hear each other directly (-30 dB,
  below WINK-N's threshold) but both A↔B and B↔C are solid (-10 dB).
  With B opted in as a relay, the message reaches C with the payload
  intact. Also confirmed: a non-relay node doesn't propagate, a
  re-heard duplicate isn't re-relayed, and a TTL=0 packet is dropped
  rather than relayed. All four checks passed.

## What's still not decided/built

- **#1 from below (chat vs. beacon-only traffic) is still open** — the
  relay logic itself doesn't care what kind of packet it's relaying,
  so this doesn't block what's built, but it matters for real design
  choices like whether beacon spots get relayed by default.
- **Routing beyond flooding** (distance-vector, source routing) — not
  needed yet since flooding doesn't require route knowledge, but the
  scaling ceiling flagged below is still real for larger networks.
- **No real multi-node RF test** — this is 3 simulated nodes over the
  AWGN channel model, not real hardware.

---

## Original design notes (for context)


## 1. What is mesh traffic, actually?

Does a mesh node relay WINK-S/N/F point-to-point chat traffic, or only
beacon-style broadcast telemetry (position/status), or both? This
changes everything downstream — chat traffic wants low latency and a
real route to a specific station; beacon/telemetry wants
store-and-forward and doesn't care about latency.

## 2. Routing strategy — pick one

- **Flooding** (like a basic LoRa mesh): every node rebroadcasts once.
  Dead simple, no routing tables, works with zero topology knowledge.
  Doesn't scale past a handful of nodes on a shared HF channel — every
  hop repeats the *entire* channel-time cost of the original
  transmission, and WINK-S packets are already ~seconds long.
- **Distance-vector** (like a tiny RIP): nodes exchange periodic
  neighbor tables, build multi-hop routes. Scales better, but needs a
  real neighbor-discovery/keepalive scheme and has classic convergence
  issues (count-to-infinity) that need a real fix (split horizon at
  minimum), not just a link report reused for a different purpose.
- **Source routing**: originator specifies the hop path. Needs the
  path known in advance (from a prior discovery flood or manual
  config) but keeps intermediate nodes stateless — plausible fit for a
  small, mostly-static set of fixed stations, bad fit for anything
  mobile/dynamic.

Given WINK's actual likely topology (a handful of amateur stations,
probably mostly fixed, HF propagation already doing a lot of the
"long-range hop" work for free) — **source routing over a small,
manually- or beacon-seeded node table** is probably the best starting
fit, not flooding or full dynamic distance-vector. That's a
recommendation, not a decision made for you.

## 3. Loop prevention

Whatever's chosen needs one of: a hop-count TTL (simplest), a seen-packet-ID cache per node (dedup on `(source, seq)`, which the existing `Packet.seq` field already supports), or both. Flooding without both is a
guaranteed broadcast storm on shared spectrum.

## 4. What changes in the packet format

`wink_core.Packet` already carries `source`, `dest`, `seq` — enough for
dedup and addressing. It's missing a hop count / TTL field and (for
source routing) a route field. That's a small, additive change to the
existing header, not a rewrite.

## Suggested next step

Pick answers to #1 and #2 (I'd default to: point-to-point chat relay,
source routing) and I'll build the actual packet format and a
simulated multi-node test (same pattern as the beacon/waveform
bake-offs — build it, then measure it, not just design it on paper).
