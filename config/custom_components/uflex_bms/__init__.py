# uflex_bms_1 - __init__.py
"""UFLEX BMS (BP00, Enjie EMU110x): legge le celle via Bluetooth.

Ogni 15 minuti (o col pulsante Leggi ora) si collega al BMS, scrive il
comando di lettura su FF02, ricompone i 110 byte arrivati su FF01, si
scollega e manda la risposta grezza al webhook gia' usato dal package
config_celle_bms. Al BMS manda solo il comando di lettura.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

DOMAIN = "uflex_bms"
PLATFORMS = ["button", "sensor"]
RX = "0000ff01-0000-1000-8000-00805f9b34fb"
TX = "0000ff02-0000-1000-8000-00805f9b34fb"
CMD = bytes.fromhex("7E10004661000100F7C10D")
HEAD = b"\x7e\x14"
LEN = 110
WEBHOOK = "bms_celle_ginepro_k7q2x9"
INTERVAL = timedelta(minutes=15)
SIGNAL = f"{DOMAIN}_update"
_LOGGER = logging.getLogger(__name__)


def crc_ok(f: bytes) -> bool:
    c = 0
    for x in f[1:-3]:
        c ^= x << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c == int.from_bytes(f[-3:-1], "big")


class BmsReader:
    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self.hass = hass
        self.address = address
        self.lock = asyncio.Lock()
        self.last_ok = None
        self.last_try = None
        self.esito = "mai letto"
        self.durata = None
        self.rssi = None
        self.adattatore = None
        self.crc = None
        self.http = None

    async def async_read(self) -> None:
        if self.lock.locked():
            return
        async with self.lock:
            self.last_try = dt_util.now()
            t0 = time.monotonic()
            try:
                f = await self._read()
                self.crc = crc_ok(f)
                h = f.hex().upper()
                self.hass.bus.async_fire(f"{DOMAIN}_lettura", {"hex": h})
                await self._post(h)
                self.last_ok = self.last_try
                self.esito = "ok"
            except Exception as err:  # noqa: BLE001
                self.esito = f"errore: {type(err).__name__}: {err}"[:250]
                _LOGGER.warning("Lettura BMS fallita: %s", self.esito)
            self.durata = round(time.monotonic() - t0, 1)
            async_dispatcher_send(self.hass, SIGNAL)

    async def _read(self) -> bytes:
        dev = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if dev is None:
            raise RuntimeError("BMS non visibile da nessun adattatore")
        si = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        if si:
            self.rssi = si.rssi
            self.adattatore = si.source
        buf = bytearray()
        done = asyncio.Event()

        def on_data(_char, data: bytearray) -> None:
            buf.extend(data)
            i = buf.find(HEAD)
            if i >= 0 and len(buf) - i >= LEN:
                done.set()

        client = await establish_connection(
            BleakClientWithServiceCache, dev, "UFLEX BMS", max_attempts=3
        )
        try:
            await client.start_notify(RX, on_data)
            await asyncio.sleep(0.8)
            ch = client.services.get_characteristic(TX)
            resp = not (ch and "write-without-response" in ch.properties)
            await client.write_gatt_char(TX, CMD, response=resp)
            await asyncio.wait_for(done.wait(), 15)
        finally:
            await client.disconnect()
        i = buf.find(HEAD)
        f = bytes(buf[i : i + LEN])
        if f[-1] != 0x0D:
            raise ValueError(f"risposta non valida: {f.hex()[:40]}")
        return f

    async def _post(self, h: str) -> None:
        url = f"http://127.0.0.1:{self.hass.http.server_port}/api/webhook/{WEBHOOK}"
        s = async_get_clientsession(self.hass)
        async with s.post(url, json={"hex": h}, timeout=10) as r:
            self.http = r.status


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    reader = BmsReader(hass, entry.data[CONF_ADDRESS])
    entry.runtime_data = reader
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def tick(_now) -> None:
        hass.async_create_background_task(reader.async_read(), "uflex_bms_read")

    entry.async_on_unload(async_track_time_interval(hass, tick, INTERVAL))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
