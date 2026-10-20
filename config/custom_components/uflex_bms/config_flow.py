# uflex_bms_1 - config_flow.py
import voluptuous as vol

from homeassistant.components.bluetooth import async_discovered_service_info
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS

from . import DOMAIN


class UflexBmsFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._addr = None
        self._name = None

    async def async_step_bluetooth(self, info):
        await self.async_set_unique_id(info.address)
        self._abort_if_unique_id_configured()
        self._addr = info.address
        self._name = info.name
        self.context["title_placeholders"] = {"name": f"{info.name} {info.address}"}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="UFLEX BMS", data={CONF_ADDRESS: self._addr}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._name, "address": self._addr},
        )

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            a = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(a, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="UFLEX BMS", data={CONF_ADDRESS: a})
        found = {
            i.address: f"{i.name} {i.address}"
            for i in async_discovered_service_info(self.hass, connectable=True)
            if i.name == "BP00"
        }
        if not found:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(found)}),
        )
