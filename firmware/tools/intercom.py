#!/usr/bin/env python3
"""Bench client for the intercom module.

  intercom.py scan                 print every advert, timestamped
  intercom.py set none|deny|allow
  intercom.py set until SECONDS    allow for SECONDS from now
  intercom.py set times N          allow the next N rings
  intercom.py relay door|handset ... [--hold S]
                                   debug: hold the listed relays on for S
                                   seconds (default 1), then all off; the
                                   firmware drops them when we disconnect
  intercom.py bootloader           reboot into the UF2 bootloader (plug USB in
                                   first), then flash.sh as after a double-tap
  intercom.py --selftest           decode/encode check, no radio
"""
import argparse
import asyncio
import struct
import time

COMPANY = 0xFFFF
MAGIC = b"IC"
CTRL = "ce780001-18cf-43f4-97fa-9fc4443b48e5"
MODES = {"none": 0, "deny": 1, "allow": 2, "until": 3, "times": 4}
RELAYS = {"door": 1, "handset": 2}
OP_RELAYS = 0x10
OP_BOOTLOADER = 0x20
SESSION_REFRESH_S = 5  # under the firmware's 10 s idle timeout
STATE = struct.Struct("<BBBHHHIH")  # ver mode flags ring open deny remaining batt_mv


def decode(mfg: bytes) -> dict | None:
    """Manufacturer data after the company ID, as bleak hands it over."""
    if not mfg.startswith(MAGIC) or len(mfg) < 2 + STATE.size:
        return None
    ver, mode, flags, ring, opened, denied, remaining, mv = STATE.unpack_from(mfg, 2)
    return dict(ver=ver, mode=mode, ringing=flags & 1, ring=ring, open=opened,
                deny=denied, remaining=remaining, battery_mv=mv)


def encode(mode: str, arg: int | None) -> bytes:
    if MODES[mode] >= 3:
        if not arg:
            raise SystemExit(f"{mode} needs a positive number")
        return struct.pack("<BI", MODES[mode], arg)
    return bytes([MODES[mode]])


def encode_relays(names: list[str]) -> bytes:
    mask = 0
    for n in names:
        mask |= RELAYS[n]
    return bytes([OP_RELAYS, mask])


def ours(adv) -> dict | None:
    return decode(adv.manufacturer_data.get(COMPANY, b""))


async def scan():
    from bleak import BleakScanner

    def seen(dev, adv):
        if (s := ours(adv)) is not None:
            print(f"{time.monotonic():10.3f} {dev.address} rssi={adv.rssi} {s}", flush=True)

    async with BleakScanner(seen):
        await asyncio.Event().wait()


async def find(timeout: float):
    from bleak import BleakScanner

    dev = await BleakScanner.find_device_by_filter(lambda d, a: ours(a) is not None, timeout)
    if dev is None:
        raise SystemExit("intercom not found")
    return dev


async def set_mode(payload: bytes, timeout: float):
    from bleak import BleakClient

    dev = await find(timeout)
    async with BleakClient(dev) as client:
        await client.write_gatt_char(CTRL, payload, response=True)
    print(f"wrote {payload.hex()} to {dev.address}")


async def relay(names: list[str], hold: float, timeout: float):
    from bleak import BleakClient

    dev = await find(timeout)
    on = encode_relays(names)
    async with BleakClient(dev) as client:
        print(f"{dev.address}: relays {on.hex()} for {hold} s")
        end = time.monotonic() + hold
        while (left := end - time.monotonic()) > 0:
            # Rewriting the same mask keeps the session from idling out.
            await client.write_gatt_char(CTRL, on, response=True)
            await asyncio.sleep(min(left, SESSION_REFRESH_S))
        await client.write_gatt_char(CTRL, encode_relays([]), response=True)
    print("relays off")


def selftest():
    mfg = MAGIC + STATE.pack(1, 4, 1, 7, 3, 2, 5, 4012)
    assert decode(mfg) == dict(ver=1, mode=4, ringing=1, ring=7, open=3, deny=2,
                               remaining=5, battery_mv=4012)
    assert decode(b"XX" + mfg[2:]) is None and decode(mfg[:-1]) is None
    assert encode("deny", None) == b"\x01"
    assert encode("until", 3600) == b"\x03\x10\x0e\x00\x00"
    assert encode("times", 2) == b"\x04\x02\x00\x00\x00"
    assert encode_relays([]) == b"\x10\x00"
    assert encode_relays(["handset", "door"]) == b"\x10\x03"
    print("ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["scan", "set", "relay", "bootloader"])
    ap.add_argument("args", nargs="*", help="mode [number] for set, relay names for relay")
    ap.add_argument("--hold", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.cmd == "scan":
        asyncio.run(scan())
    elif a.cmd == "set" and 1 <= len(a.args) <= 2 and a.args[0] in MODES:
        arg = int(a.args[1]) if len(a.args) == 2 else None
        asyncio.run(set_mode(encode(a.args[0], arg), a.timeout))
    elif a.cmd == "bootloader":
        asyncio.run(set_mode(bytes([OP_BOOTLOADER]), a.timeout))
    elif a.cmd == "relay" and a.args and set(a.args) <= set(RELAYS):
        asyncio.run(relay(a.args, a.hold, a.timeout))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
