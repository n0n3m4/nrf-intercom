"""Config flow: the module needs no pairing and no secret, so setup is just
picking an address."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from . import protocol
from .const import DOMAIN, MANUFACTURER


def _ours(info: BluetoothServiceInfoBleak) -> bool:
    return protocol.decode(info.manufacturer_data.get(protocol.COMPANY, b"")) is not None


class IntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Bluetooth discovery, or a pick from the modules seen so far."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        # Company 0xFFFF is "unassigned": the matcher's magic bytes are a weak
        # filter, so check the whole payload.
        if not _ours(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.address}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        address = self._discovery.address
        if user_input is not None:
            return self.async_create_entry(
                title=f"{MANUFACTURER} {address}", data={CONF_ADDRESS: address}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders={"name": address}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"{MANUFACTURER} {address}", data={CONF_ADDRESS: address}
            )

        configured = self._async_current_ids()
        candidates = {
            info.address: f"{info.name or MANUFACTURER} ({info.address})"
            for info in async_discovered_service_info(self.hass)
            if info.address not in configured and _ours(info)
        }
        if not candidates:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(candidates)}),
        )
