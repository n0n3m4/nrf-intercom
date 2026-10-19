"""Turns the module's adverts into state and events, and keeps its mode in sync.

Everything is read from passive adverts. The only connection is the CTRL write
that sets the mode: the module boots in NONE on purpose, so HA owns the mode it
should be in and writes it again whenever the advert disagrees.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.event import async_call_later

from . import protocol
from .const import DOMAIN, MANUFACTURER, MODEL

_LOGGER = logging.getLogger(__name__)

# After every write, look again this much later, and write again if the advert
# still disagrees. HA drops adverts identical to the last one, so a failed or
# unconfirmed write cannot wait for the next advert to retry it.
RETRY_S = 30

# advert counter -> event type
COUNTERS = {"ring": "ring", "open": "opened", "deny": "denied"}


class IntercomCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Latest decoded advert, the events it carried, and the wanted mode."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, address: str) -> None:
        super().__init__(
            hass, _LOGGER, address, bluetooth.BluetoothScanningMode.PASSIVE, connectable=True
        )
        self.entry = entry
        self.state: dict | None = None
        # Event types carried by the advert being dispatched right now.
        self.events: list[str] = []
        self.desired = protocol.Desired()
        # Number entities' values, for picking a timed mode from the select.
        self.allow_minutes = 60.0
        self.allow_rings = 1.0
        self._force = False
        self._task: asyncio.Task | None = None
        self._retry: Callable[[], None] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.address)},
            connections={(CONNECTION_BLUETOOTH, self.address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=f"{MANUFACTURER} {self.address}",
        )

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        adv = protocol.decode(service_info.manufacturer_data.get(protocol.COMPANY, b""))
        if adv is None:
            return
        old, self.state = self.state, adv
        deltas = {k: adv[k] - old[k] for k in COUNTERS} if old else {}
        # No baseline yet, or a counter went down (the module rebooted):
        # resync without firing anything.
        if any(d < 0 for d in deltas.values()):
            deltas = {}
        self.events = [COUNTERS[k] for k, d in deltas.items() for _ in range(d)]
        if not self._force:
            self.desired = protocol.advance(
                self.desired, adv, deltas.get("open", 0), time.time()
            )
        super()._async_handle_bluetooth_event(service_info, change)
        self.events = []
        self._sync()

    @callback
    def async_restore(self, desired: protocol.Desired) -> None:
        """The select's last state, after a restart."""
        self.desired = protocol.advance(desired, {"mode": None}, 0, time.time())
        self._sync()

    @callback
    def async_set_mode(self, mode: int) -> None:
        now = time.time()
        self.desired = protocol.Desired(
            mode, now + self.allow_minutes * 60, int(self.allow_rings)
        )
        self._force = True
        self.async_update_listeners()
        self._sync()

    @callback
    def _sync(self) -> None:
        if self._task is not None and not self._task.done():
            return  # it re-checks when its retry fires
        payload = protocol.command(self.desired, self.state, time.time(), self._force)
        if payload is None:
            return
        if self._retry is not None:
            self._retry()
            self._retry = None
        self._task = self.entry.async_create_background_task(
            self.hass, self._write(payload), f"{DOMAIN} write {self.address}"
        )

    async def _write(self, payload: bytes) -> None:
        try:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is None:
                raise RuntimeError("not seen by a connectable scanner")
            client = await establish_connection(
                BleakClientWithServiceCache, device, self.address
            )
            try:
                await client.write_gatt_char(protocol.CTRL, payload, response=True)
            finally:
                await client.disconnect()
        except Exception as err:  # noqa: BLE001 -- bleak errors are not OSError
            _LOGGER.debug("%s: writing %s failed: %s", self.address, payload.hex(), err)
        else:
            _LOGGER.debug("%s: wrote %s", self.address, payload.hex())
            self._force = False

        @callback
        def _again(_now) -> None:
            self._retry = None
            self._sync()

        self._retry = async_call_later(self.hass, RETRY_S, _again)

    async def async_stop(self) -> None:
        if self._retry is not None:
            self._retry()
        if self._task is not None:
            self._task.cancel()


class IntercomEntity(PassiveBluetoothCoordinatorEntity[IntercomCoordinator]):
    """One entity of the module's device, named by its translation key."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IntercomCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = coordinator.device_info
