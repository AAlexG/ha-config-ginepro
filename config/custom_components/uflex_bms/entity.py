# uflex_bms_1 - entity.py
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import DOMAIN, SIGNAL


class BmsEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, reader, key: str, name: str) -> None:
        self.reader = reader
        a = reader.address
        self._attr_unique_id = f"{a}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, a)},
            connections={(CONNECTION_BLUETOOTH, a)},
            name="UFLEX BMS",
            manufacturer="UFLEX",
            model="BMS BP00",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state)
        )
