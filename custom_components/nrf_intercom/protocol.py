"""Intercom module wire protocol, and what to write to keep its mode in sync.

No Home Assistant imports: ``python protocol.py`` runs the self-checks. The
encoding is the one in ``firmware/README.md`` and ``firmware/tools/intercom.py``;
change all of them together.
"""

from __future__ import annotations

import struct
from bisect import bisect
from typing import NamedTuple

COMPANY = 0xFFFF
MAGIC = b"IC"
CTRL = "ce780001-18cf-43f4-97fa-9fc4443b48e5"
PROTOCOL_VERSION = 1
STATE = struct.Struct("<BBBHHHIH")  # ver mode flags ring open deny remaining batt_mv

NONE, DENY, ALLOW, ALLOW_UNTIL, ALLOW_N = range(5)
MODES = ("none", "deny", "allow", "allow_for", "allow_times")

# The device's clock and ours drift apart, so a timed window closing within
# this many seconds counts as closed: otherwise a device that fell back to NONE
# a moment early would be sent a few seconds of ALLOW, a connection for nothing.
# Its 32 kHz crystal drifts ~2 s a day; it must stay well under the shortest
# window (1 min), or allow_for lapses as soon as it is written.
UNTIL_SLACK_S = 10

# Calibration knob: a resting LiPo's open-circuit voltage at 0, 10 ... 100 %,
# Meshtastic's OCV_ARRAY reversed. Linear between points.
BATTERY_OCV_MV = (3100, 3300, 3420, 3530, 3630, 3720, 3800, 3890, 3990, 4050, 4190)


def battery_percent(mv: int) -> int:
    i = bisect(BATTERY_OCV_MV, mv)
    if i == 0:
        return 0
    if i == len(BATTERY_OCV_MV):
        return 100
    lo, hi = BATTERY_OCV_MV[i - 1], BATTERY_OCV_MV[i]
    return round((i - 1 + (mv - lo) / (hi - lo)) * 10)


class Desired(NamedTuple):
    """The mode HA wants. ``deadline`` is a unix time, for ALLOW_UNTIL;
    ``count`` the opens left, for ALLOW_N."""

    mode: int = NONE
    deadline: float = 0.0
    count: int = 0


def decode(mfg: bytes) -> dict | None:
    """Manufacturer data after the company ID, or None if it is not ours."""
    if not mfg.startswith(MAGIC) or len(mfg) < 2 + STATE.size:
        return None
    ver, mode, flags, ring, opened, denied, remaining, mv = STATE.unpack_from(mfg, 2)
    if ver != PROTOCOL_VERSION:
        return None
    return dict(mode=mode, ringing=bool(flags & 1), ring=ring, open=opened,
                deny=denied, remaining=remaining, battery_mv=mv)


def encode(mode: int, arg: int = 0) -> bytes:
    if mode in (ALLOW_UNTIL, ALLOW_N):
        return struct.pack("<BI", mode, max(1, arg))
    return bytes([mode])


def advance(want: Desired, adv: dict, opens: int, now: float) -> Desired:
    """Let a timed mode run down: ``opens`` is how many doors opened since the
    last advert. A spent mode becomes NONE, as the device does by itself."""
    mode, deadline, count = want
    if mode == ALLOW_N:
        # The device's own count wins while it has one; the open that spends
        # it arrives in the same advert as its fallback to NONE.
        count = adv["remaining"] if adv["mode"] == ALLOW_N else count - opens
        if count <= 0:
            mode = NONE
    elif mode == ALLOW_UNTIL and deadline - now < UNTIL_SLACK_S:
        mode = NONE
    return Desired(mode, deadline, count)


def command(want: Desired, adv: dict | None, now: float, force: bool) -> bytes | None:
    """CTRL payload to send, or None while the device already agrees."""
    if not force and adv is not None and adv["mode"] == want.mode:
        return None
    if want.mode == ALLOW_UNTIL:
        return encode(ALLOW_UNTIL, int(want.deadline - now))
    return encode(want.mode, want.count)


def _selftest() -> None:
    mfg = MAGIC + STATE.pack(1, 4, 1, 7, 3, 2, 5, 4012)
    adv = decode(mfg)
    assert adv == dict(mode=4, ringing=True, ring=7, open=3, deny=2,
                       remaining=5, battery_mv=4012)
    assert decode(b"XX" + mfg[2:]) is None and decode(mfg[:-1]) is None
    assert decode(MAGIC + STATE.pack(2, 4, 1, 7, 3, 2, 5, 4012)) is None
    assert encode(DENY) == b"\x01"
    assert encode(ALLOW_UNTIL, 3600) == b"\x03\x10\x0e\x00\x00"
    assert encode(ALLOW_N, 2) == b"\x04\x02\x00\x00\x00"

    def state(mode, remaining=0):
        return dict(adv, mode=mode, remaining=remaining)

    # ALLOW_N follows the device, and the last open spends it without a resend.
    n = Desired(ALLOW_N, count=3)
    assert advance(n, state(ALLOW_N, 2), 1, 0) == Desired(ALLOW_N, count=2)
    spent = advance(Desired(ALLOW_N, count=1), state(NONE), 1, 0)
    assert spent.mode == NONE and command(spent, state(NONE), 0, False) is None
    # Reboot: device back in NONE, nothing opened -> resend what is left.
    after = advance(Desired(ALLOW_N, count=2), state(NONE), 0, 0)
    assert command(after, state(NONE), 0, False) == encode(ALLOW_N, 2)
    # ALLOW_UNTIL resends the seconds left, and lapses just before its deadline.
    u = Desired(ALLOW_UNTIL, deadline=1000.0)
    assert advance(u, state(NONE), 0, 100) == u
    assert command(u, state(NONE), 100, False) == encode(ALLOW_UNTIL, 900)
    assert advance(u, state(NONE), 0, 995).mode == NONE
    # The shortest window survives the write that sets it.
    one = Desired(ALLOW_UNTIL, deadline=100.0 + 60)
    assert advance(one, state(ALLOW_UNTIL, 55), 0, 105) == one
    # Agreement means silence unless forced; no advert yet means send.
    assert command(Desired(ALLOW), state(ALLOW), 0, False) is None
    assert command(Desired(ALLOW), state(ALLOW), 0, True) == b"\x02"
    assert command(Desired(DENY), None, 0, False) == b"\x01"
    # Battery % follows the OCV table, clamped at both ends.
    assert [battery_percent(mv) for mv in (0, 3100, 3800, 3845, 4090, 4190, 4400)] == [
        0, 0, 60, 65, 93, 100, 100]
    print("ok")


if __name__ == "__main__":
    _selftest()
