# NRF-INTERCOM

A battery-powered BLE module that sits on an apartment intercom handset: it
detects ringing and, per a mode set over BLE, does nothing, rejects the call or
opens the door. Two parts:

- `firmware/` — Pro Micro nRF52840, Zephyr / nRF Connect SDK v3.3.0. Done.
- Home Assistant integration — not started; it consumes the protocol in
  `firmware/README.md` (passive adverts for state/events, a GATT write for the mode).

Read `firmware/README.md` for wiring, modes, timings and the protocol table;
none of that is repeated here.

## Always rebuild

**After every firmware change, rebuild with `firmware/build.sh`.** It copies
the image to `firmware/intercom.uf2`, which is what gets flashed: the file in
the firmware root must always match the sources. A change is not finished
until the build is clean (no warnings) and that file is fresh.

## Commands

```sh
firmware/build.sh                    # release -> firmware/intercom.uf2
firmware/build.sh debug              # printk on USB CDC ACM (keeps HFXO on)
firmware/build.sh [debug] --pristine # after switching flavours or Kconfig/DT changes
firmware/flash.sh                    # double-tap RST first
python3 firmware/tools/intercom.py --selftest   # protocol encode/decode, no radio
```

Never pass `-d` to `build.sh`: it already passes `-d firmware/build`, and a
second `-d` wins and is resolved relative to `~/ncs/v3.3.0`, littering the SDK.

## Lineage

The board setup is copied from `~/espaper/nrf-port` (the e-paper project on the
same board): `build.sh`/`flash.sh`/`debug.conf`, the partition table (app at
0x1000 because the bootloader has no SoftDevice, image must stay below
0xAD000), USB off in release, 32 kHz crystal LFCLK, +8 dBm. Its `CLAUDE.md`
"Things that bite" applies here too; read it before touching Kconfig or the
overlay.

## Firmware design, in one place: `firmware/src/main.c`

- **Power**: System ON idle with slow connectable adverts (1–1.5 s). System OFF
  would take BLE down, so "sleep" is the SDC sleeping CPU+HFXO between events;
  LPCOMP wakes it on a ring.
- **Regulators**: both DC/DC stages are on (`&reg1` mode DCDC, `&reg0` okay in
  `app.overlay`; Zephyr's `soc.c` applies them at boot). Each needs its inductor
  on the board. A board that stops booting after a flash: double-tap RST,
  reflash, and drop the stage whose inductor is missing.
- **Threads**: the LPCOMP ISR and GATT callbacks only set flags / queue a
  `struct cmd` and give `s_event`. `main()` alone touches relays, mode and
  advertising. GATT writes go through the `s_cmds` msgq so a command arriving
  mid-action waits for it.
- **Ring window**: one window does two jobs. A ring starts on the first edge,
  every later edge only extends it, and it ends `RING_GAP_MS` after the last
  edge — but never while an action runs (`s_acting`) and never sooner than
  `RING_GAP_MS` after it (`ring_action()` re-arms `s_ring_end`). That is both
  the cadence debounce and the guard against our own relays retriggering. Do
  not add a separate ignore/guard timer.
- **Actions**: DENY = handset up `PICKUP_MS`, down (the door button is not
  touched). ALLOW = handset up, button pulse, handset down. ALLOW_UNTIL /
  ALLOW_N fall back to NONE when spent. Boot mode is NONE by design: HA
  re-sends its mode on mismatch, so there is no flash storage.
- **Adverts**: state is manufacturer data (company 0xFFFF, magic "IC"). Events
  carry counters, not a "last event" byte, so two events in one advert interval
  both arrive. Every event (ring start/end, open, deny, mode change) restarts
  advertising fast (30–60 ms) for `FAST_ADV_BURST_MS`; battery and countdown
  updates only update data. Interval changes need stop/start, which
  `adv_apply()` owns; `F_ADV_LOST` tells it a connection stopped the advert.
- **Debug relays**: CTRL `[0x10, mask]` drives the relays by hand. Deliberately
  kept in release (it only duplicates the physical buttons); held only while
  the client stays connected, cleared by a ring action. Not an event.
- **Battery**: SAADC VDDH/5 — real on nice!nano-v2-style boards where B+ feeds
  VDDH. Firmware sends mV; % mapping belongs to the HA side.

## Calibration knobs

Real hardware, real intercom: the timings at the top of `main.c` and the LPCOMP
`refsel` in `app.overlay` are meant to be tuned on the bench, not "cleaned up".

## Protocol changes

The protocol lives in three places: `firmware/src/main.c`, the table in
`firmware/README.md`, and `firmware/tools/intercom.py` (plus its `--selftest`).
Change all three together, and later the HA integration. Bump
`PROTOCOL_VERSION` for incompatible changes.

## Git

One repo for the whole project, branch `main`, commit straight to it.
`firmware/intercom.uf2` is committed on purpose: it is the flashable image and
must match the sources in the same commit.
