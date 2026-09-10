# Winkdigimode
# WINK: Building A Weak-Signal Digital Mode That Wants To Actually Have A Conversation

**What happens when you take the weak-signal tricks of modern digital radio and combine them with the user experience of a messaging app?**

I've been building WINK to find out.

Amateur radio has no shortage of digital modes. We've got modes designed to push text through narrow slices of spectrum, modes designed to dig incredibly weak signals out of the noise, and networking systems designed to move messages around the world.

But they tend to make different compromises.

PSK31 is a particularly interesting example. It was designed around the idea of keyboard-to-keyboard communication, squeezing roughly 31 bits/s into an extremely narrow signal. It remains a remarkably elegant piece of radio engineering.

FT8 goes in almost the opposite direction: give the computer a tiny, carefully engineered signal and let it extract a contact from conditions where conventional communication would be difficult.

So I started asking a slightly annoying question:

**Why can't weak-signal digital radio be good at actually having a conversation?**

That became WINK.

---

## Not Another Modem... Hopefully

The temptation when designing a new digital mode is to start with modulation.

8-FSK!
QPSK!
OFDM!
Some horrifying combination of all three!

WINK started there too.

And immediately demonstrated why that's a bad idea.

The first prototype used 8-FSK at 15 baud with only 2.5 Hz between tones. On paper, that looked wonderfully narrow.

In practice, it was a disaster.

The adjacent tones were simply too close together for the non-coherent tone detector being used. At 15 baud, the symbol duration is about 66.7 ms, and the original 2.5 Hz spacing was nowhere near enough to keep the tones behaving independently.

The result was a modem that fell over somewhere between -5 and -10 dB in the initial test.

No amount of clever FEC was going to fix that.

The problem wasn't the decoder.

**The waveform was wrong.**

And that's where the project got interesting.

---

## Stop Guessing. Build A Bake-Off.

Rather than tweaking one waveform until it looked good, I wrote a common sample-domain simulator and started throwing candidates at it.

Every candidate gets the same treatment:

* 12 kHz sample rate
* continuous-phase audio synthesis
* the same packet framing
* the same convolutional FEC
* the same interleaver
* the same CRC
* AWGN added at the waveform level
* carrier-frequency offset
* unknown symbol timing
* actual synchronization and demodulation

Most importantly, the noise model is shared.

The first bake-off was actually invalid because I had accidentally given some candidates an artificial processing-gain advantage. The simulator produced essentially perfect results for everything and therefore told me nothing.

So that experiment went in the bin.

That's an important part of WINK's design philosophy:

> **If the simulation can't distinguish between designs, the simulation is broken.**

Fix the experiment, not the graph.

The corrected bake-off produced a much more interesting result.

The original 8-FSK/15-baud/2.5-Hz design collapsed early.

A 4-FSK candidate did considerably better.

But the really interesting candidates were slower 8-FSK designs with sensible tone spacing.

The best candidate in the extended test reached approximately a 50% packet-success threshold around **-23.5 dB** in the simulated AWGN channel.

That's not an over-the-air claim.

It's not "WINK beats FT8."

It's a measurement showing that the waveform and coding combination is at least worth taking to the next experiment.

And that distinction matters.

---

## WINK-S, WINK-N And WINK-F

The next idea was that there shouldn't necessarily be one WINK modem.

HF propagation isn't constant.

Why force the operator to choose between "slow but robust" and "fast but fragile" when the software can measure the link?

So WINK currently has three profiles:

| Profile    | Baud | Spacing | Bandwidth | Coded rate |
| ---------- | ---: | ------: | --------: | ---------: |
| **WINK-S** |    5 |  7.5 Hz |     60 Hz |  7.5 bit/s |
| **WINK-N** |   15 |   15 Hz |    120 Hz | 22.5 bit/s |
| **WINK-F** |   30 |   30 Hz |    240 Hz |   45 bit/s |

The simulated 50%-success points are approximately -23.5, -18.5 and -15.5 dB respectively. In other words, roughly every threefold increase in speed costs about 5 dB of sensitivity in this particular simulation.

That's not a free lunch.

And that's exactly the point.

WINK doesn't pretend the physics went away.

It gives the software a set of sensible places to operate on the speed/robustness curve.

---

## Let The Receiver Make The Decision

Here's the part I think makes WINK more interesting than simply "yet another FSK modem."

Suppose two stations establish a link using WINK-S.

The receiving station can report something like:

> SNR: -19 dB
> Decode quality: good
> Recommended profile: WINK-N

The transmitter can then move to WINK-N.

If propagation improves further, it can eventually move toward WINK-F.

If the band collapses again, it goes the other way.

There is hysteresis in the profile selection so it doesn't bounce back and forth every time the measured SNR moves by half a decibel.

I ran the entire chain together — packets, modem, link reports and adaptive profile selection — and the simulated QSO successfully moved S → N as conditions improved, then returned toward S as the channel deteriorated.

The important bit is that the operator doesn't need to understand any of this.

Ideally, they just see:

**Connected.**

---

## The FEC Is Doing Real Work Too

WINK uses convolutional coding and interleaving, with soft-decision decoding.

The current default is a K=9, rate-1/2 convolutional code.

Instead of reducing every received symbol to a simple "yes, that was a 1" or "no, that was a 0", the decoder gets information about how confident the demodulator was.

That's useful when you're living down near the noise floor.

The current simulations put the K9 + soft-decision version at approximately **-24.3 dB** for WINK-S under the project's AWGN test conditions.

Again: simulated channel, not an RF performance guarantee.

That's an important theme throughout this project.

I'm much more interested in saying:

**"Here are 60 trials at each point."**

than:

**"This modem is really sensitive."**

---

## The User Interface Is Part Of The Experiment

There's another problem with a lot of digital radio software.

The operator is still expected to understand the modem.

WINK is intended to reverse that relationship.

Imagine tuning a radio to a fixed WINK channel.

Instead of manually hunting around the waterfall, the receiver watches the entire channel.

Maybe it finds:

```text
VK3ABC     -18 dB     WINK-S     -120 Hz
JA1XYZ     -12 dB     WINK-N      +95 Hz
VK2DEF      -7 dB     WINK-F     +180 Hz
```

Click JA1XYZ.

WINK knows where that station is.

The next transmission goes to the appropriate audio offset automatically.

The modem doesn't need to know anything about the user interface, and the UI doesn't need to know how the modem actually performs its decoding.

That separation is deliberate.

It means I can completely rewrite the modem in the future without rebuilding the chat application.

---

## And Then There Is The Slightly Crazy Part

The long-term goal isn't just keyboard-to-keyboard chat.

WINK has been designed as a stack.

At the bottom is the audio modem.

Above that is packet framing.

Above that is the link layer.

Above that can eventually sit store-and-forward routing and mesh functionality.

That means a WINK packet doesn't necessarily have to go directly from my radio to yours.

A future WINK node could receive it, validate it, and forward it.

Different links could even use different WINK profiles depending on their conditions.

A short, strong link might use WINK-F.

A terrible HF hop might use WINK-S.

The networking layer shouldn't care.

---

## There Is A Beacon Too

WINK also has a compact beacon concept.

The current experimental beacon uses a much slower 4-FSK waveform with a compact payload containing station identification and location information.

In simulation, the current beacon prototype has produced successful decodes well down into the noise, although its roughly one-minute transmission time means there is plenty of room for optimization.

And that's another place where WINK could eventually become interesting.

A small standalone WINK beacon or node shouldn't need a full PC.

The eventual goal is for simple WINK hardware to be able to transmit, receive and participate in the network.

But that's future work.

---

# The Important Bit: It Hasn't Been On HF Yet

This is where I need to put the brakes on the hype.

Everything above is still simulation.

The current WINK results use an AWGN channel.

They do **not** yet include a proper HF multipath/fading model.

They haven't gone through a sound card.

They haven't gone through an HF transceiver.

And therefore I am absolutely not claiming that a simulated -24 dB result means WINK will decode at -24 dB on the air.

The project's own next steps are deliberately boring:

**model multipath and fading → test real audio I/O → compare against an established mode under controlled conditions → put it on the air.**

That's where WINK gets to prove whether it deserves to exist.

---

# So Why WINK?

I'm not trying to replace PSK31.

PSK31 already demonstrated that narrow-band keyboard communication works beautifully.

I'm trying to ask what that idea might look like if we designed it with today's computers and DSP capabilities.

The goal isn't:

> **"Look how complicated my modem is."**

It's:

> **"Look how little the operator has to care about the modem."**

The ideal WINK contact shouldn't feel like configuring a DSP experiment.

It should feel like opening a chat window.

The radio should find the stations.

The receiver should measure the link.

The system should choose an appropriate mode.

The protocol should protect the message.

And when propagation changes, the software should adapt.

Underneath all of that, of course, there is still a gloriously messy HF signal bouncing around the ionosphere.

That's the fun part.

---

## WINK's Design Rule

There's one rule I'm trying to apply to the whole project:

**Idea → simulator → thousands of transmissions → decode statistics → keep what works → discard what doesn't.**

If a fancy DSP technique produces no measurable improvement, it doesn't go into WINK.

If a simpler waveform wins the bake-off, the complicated one loses.

If the real radio disproves the simulation, the simulation loses.

And if WINK turns out to be a terrible idea once it reaches the airwaves?

Well...

**that's a perfectly good amateur-radio experiment too.**

But if it works, the goal is something rather different from another digital mode.

Not a mode where the operator has to think about the modem.

A mode where the modem gets out of the way.

### **WINK: weak-signal radio that actually feels like messaging.**

