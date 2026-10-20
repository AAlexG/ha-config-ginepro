# uflex_bms_1 - button.py
from homeassistant.components.button import ButtonEntity

from .entity import BmsEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([LeggiOra(entry.runtime_data, "leggi", "Leggi ora")])


class LeggiOra(BmsEntity, ButtonEntity):
    _attr_icon = "mdi:bluetooth-transfer"

    async def async_press(self) -> None:
        await self.reader.async_read()
