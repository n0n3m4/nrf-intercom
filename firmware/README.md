# Intercom module firmware

Pro Micro nRF52840 (nice!nano v2 pinout, Adafruit UF2 bootloader, no SoftDevice)
on Zephyr / nRF Connect SDK v3.3.0. Same board setup, build scripts and power
approach as `~/espaper/nrf-port`.

## Wiring

| nRF pin | Connected to | Meaning |
|---|---|---|
| P1.11 | Omron G3VM-61D1 (NO) LED | high = "door open" button pressed |
| P1.15 | CPC1117N (NC) LED | high = handset picked up |
| P0.02 / AIN0 | ring line via 100k/20k | 12 V square → ~2.0 V |

Both outputs use high drive (H0H1). The ring line goes to LPCOMP with a
3/8 VDD (~1.24 V) threshold and hysteresis (`app.overlay`, `refsel`): move it if
the line does not idle near 0 V. The divider must keep AIN0 inside 0..VDD;
a line that swings negative needs a clamp diode.

Battery is read as VDDH/5 by the SAADC: on nice!nano v2 clones B+ feeds VDDH
directly. A board powered through the 3.3 V pin instead reports ~3.3 V.

## Build and flash

```sh
./build.sh                 # release, USB off -> intercom.uf2 (copied from build/zephyr/zephyr.uf2)
./build.sh debug           # printk on USB CDC ACM (keeps HFXO on, not for current measurements)
./build.sh [debug] --pristine   # after switching flavours or touching Kconfig/DT
./flash.sh                 # double-tap RST first
```

## Behaviour

On the first ring edge the device reports `ringing` and, after
`RING_ACTION_DELAY_MS`, does what the mode says:

| Mode | Action |
|---|---|
| 0 NONE | report only |
| 1 DENY | handset up `PICKUP_MS`, down |
| 2 ALLOW | handset up, `PICKUP_MS`, button `PRESS_MS`, `HANGUP_MS`, handset down |
| 3 ALLOW_UNTIL | ALLOW until the deadline, then NONE |
| 4 ALLOW_N | ALLOW for N rings, then NONE |

A ring ends `RING_GAP_MS` after the last edge, and never while an action runs
or sooner than `RING_GAP_MS` after it. Edges inside the ring only extend it, so
the ring cadence, the relays and the intercom's reaction to them cannot start a
second ring; `RING_GAP_MS` must exceed any gap in the cadence. All timings are at the
top of `src/main.c`. Boot mode is NONE; the HA side re-sends its mode when the
advertised one differs.

Idle is System ON with connectable adverts every 1–1.5 s. Every event (ring
start/end, open, deny, mode change) restarts advertising at 30–60 ms for 3 s
so a scanner sees it immediately; battery updates (every minute) do not.

## Protocol

**Advertising** — manufacturer data, company `0xFFFF`, little-endian:

| Offset | Type | Field |
|---|---|---|
| 0 | 2×u8 | magic `'I' 'C'` |
| 2 | u8 | protocol version (1) |
| 3 | u8 | mode |
| 4 | u8 | flags, bit 0 = ringing |
| 5 | u16 | ring count |
| 7 | u16 | open count |
| 9 | u16 | deny count |
| 11 | u32 | ALLOW_UNTIL: seconds left at last update; ALLOW_N: opens left; else 0 |
| 15 | u16 | battery mV |

Counters start at 0 on boot. A client fires one event per increment; a counter
going down means a reboot, so resync without firing.

**GATT** — service `ce780000-18cf-43f4-97fa-9fc4443b48e5`:

| Characteristic | Properties | Value |
|---|---|---|
| `ce780001-…` CTRL | write | `[mode]`, or `[3, seconds u32]`, `[4, count u32]`; debug `[0x10, mask]`; anything else → ATT error 0x13 |
| `ce780002-…` STATE | read, notify | advert bytes 2..16 |

No pairing. A client idle for 10 s is disconnected.

Debug `[0x10, mask]` drives the relays by hand: bit 0 door (P1.11), bit 1
handset (P1.15). It only duplicates the intercom's own buttons, so it is in
release builds too. The relays drop when the client disconnects (or idles out),
and a ring action overrides and clears them. Not an event: no counters, no
advert burst.

## Bench client

```sh
pip install -r tools/requirements.txt
python tools/intercom.py scan
python tools/intercom.py set allow
python tools/intercom.py set until 3600
python tools/intercom.py set times 2
python tools/intercom.py relay handset --hold 3     # debug: handset up 3 s
python tools/intercom.py relay handset door        # both for 1 s
python tools/intercom.py --selftest   # no radio
```
