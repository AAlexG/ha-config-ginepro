"""Constants for the FTP integration."""

from collections.abc import Callable

from homeassistant.const import CONF_PORT
from homeassistant.util.hass_dict import HassKey

DOMAIN = "ftp"

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(
    f"{DOMAIN}.backup_agent_listeners"
)

CONF_BACKUP_PATH = "backup_path"

CONF_DEFAULTS = {
    CONF_PORT: 21,
    CONF_BACKUP_PATH: "/",
}
