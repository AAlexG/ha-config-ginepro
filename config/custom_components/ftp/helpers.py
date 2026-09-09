"""Helper functions for the FTP component."""

import logging

from aioftp import Client
from aioftp.errors import AIOFTPException
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class FtpConnection:

    def __init__(self, host: str, port: str, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._client = None

    async def __aenter__(self) -> Client:
        _LOGGER.info(
            "Start FTP connection on %s@%s:%s",
            self.username,
            self.host,
            self.port,
        )
        self._client = Client()
        await self._client.connect(self.host, self.port)
        if self.username and self.password:
            await self._client.login(self.username, self.password)
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client and not exc:
            try:
                await self._client.quit()
                _LOGGER.info(
                    "FTP connection on %s@%s:%s disconnected",
                    self.username,
                    self.host,
                    self.port,
                )
            except AIOFTPException:
                _LOGGER.debug(
                    "Failed to properly quit FTP connection on %s@%s:%s",
                    self.username,
                    self.host,
                    self.port,
                )
        self._client = None


class FtpClient:

    def __init__(self, hass: HomeAssistant, host: str, port: str, username: str, password: str):
        self.hass = hass
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    def connect(self) -> FtpConnection:
        return FtpConnection(self.host, self.port, self.username, self.password)

    def __str__(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"


async def async_ensure_path_exists(client: Client, path: str) -> bool:
    """Ensure that a path exists recursively on the FTP server."""
    if await client.exists(path):
        if not await client.is_dir(path):
            _LOGGER.error("%s exists on FTP server but is not a directory", path)
            return False
        return True

    _LOGGER.debug("%s directory does not exists on FTP server, create it", path)
    parts = path.rstrip("/").split("/")
    for i in range(1, len(parts) + 1):
        sub_path = "/" if parts[:i] == [""] else "/".join(parts[:i])
        _LOGGER.debug("Check '%s' directory on FTP server", str(sub_path))
        try:
            if await client.exists(sub_path):
                if not await client.is_dir(sub_path):
                    _LOGGER.error("%s exists on FTP server but is not a directory", sub_path)
                    return False
                _LOGGER.debug("%s directory already exists on FTP server", sub_path)
            elif await client.make_directory(sub_path):
                _LOGGER.debug("%s directory created on FTP server", sub_path)
            else:
                _LOGGER.error("Failed to create %s directory on FTP server", sub_path)
                return False
        except AIOFTPException:
            _LOGGER.exception("Failed to ensure if %s directory exists on FTP server", sub_path)
            return False

    _LOGGER.info("%s directory create on FTP server", path)
    return True
