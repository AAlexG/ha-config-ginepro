# uflex_bms_1 - sensor.py
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import BmsEntity


async def async_setup_entry(hass, entry, async_add_entities):
    r = entry.runtime_data
    async_add_entities(
        [Ultima(r, "ultima", "Ultima lettura"), Esito(r, "esito", "Esito lettura")]
    )


class Ultima(BmsEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        return self.reader.last_ok


class Esito(BmsEntity, SensorEntity):
    _attr_icon = "mdi:bluetooth-settings"

    @property
    def native_value(self):
        return self.reader.esito[:250]

    @property
    def extra_state_attributes(self):
        r = self.reader
        return {
            "ultimo_tentativo": r.last_try.isoformat() if r.last_try else None,
            "durata_s": r.durata,
            "rssi": r.rssi,
            "adattatore": r.adattatore,
            "crc_ok": r.crc,
            "http": r.http,
        }
