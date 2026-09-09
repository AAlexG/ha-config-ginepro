"""Support for FTP backup."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from functools import wraps
from time import time
from typing import Any, Concatenate

from aioftp.errors import AIOFTPException
from aiohttp import ClientTimeout
from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    suggested_filename,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.json import json_dumps
from homeassistant.util.json import json_loads_object
from propcache.api import cached_property

from . import FtpConfigEntry
from .const import CONF_BACKUP_PATH, DATA_BACKUP_AGENT_LISTENERS, DOMAIN
from .helpers import FtpConnection

_LOGGER = logging.getLogger(__name__)

BACKUP_TIMEOUT = ClientTimeout(connect=10, total=43200)
CACHE_TTL = 300


async def async_get_backup_agents(
    hass: HomeAssistant,
) -> list[BackupAgent]:
    """Return a list of backup agents."""
    entries: list[FtpConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    return [FtpBackupAgent(hass, entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Register a listener to be called when agents are added or removed.

    :return: A function to unregister the listener.
    """
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        """Remove the listener."""
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


def handle_backup_errors[_R, **P](
    func: Callable[Concatenate[FtpBackupAgent, P], Coroutine[Any, Any, _R]],
) -> Callable[Concatenate[FtpBackupAgent, P], Coroutine[Any, Any, _R]]:
    """Handle backup errors."""

    @wraps(func)
    async def wrapper(self: FtpBackupAgent, *args: P.args, **kwargs: P.kwargs) -> _R:
        try:
            return await func(self, *args, **kwargs)
        except AIOFTPException as err:
            _LOGGER.debug("Full error: %s", err, exc_info=True)
            raise BackupAgentError(
                f"Backup operation failed: {err}",
            ) from err
        except TimeoutError as err:
            _LOGGER.error(
                "Error during backup in %s: Timeout",
                func.__name__,
            )
            raise BackupAgentError("Backup operation timed out") from err

    return wrapper


def suggested_filenames(backup: AgentBackup) -> tuple[str, str]:
    """Return the suggested filenames for the backup and metadata."""
    base_name = suggested_filename(backup).rsplit(".", 1)[0]
    return f"{base_name}.tar", f"{base_name}.metadata.json"


class FtpBackupAgent(BackupAgent):
    """Backup agent interface."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: FtpConfigEntry) -> None:
        """Initialize the FTP backup agent."""
        super().__init__()
        self._hass = hass
        self._entry = entry
        self._client = entry.runtime_data
        self.name = entry.title
        self.unique_id = entry.entry_id
        self._cache_metadata_files: dict[str, AgentBackup] = {}
        self._cache_expiration = time()

    @cached_property
    def _backup_path(self) -> str:
        """Return the path to the backup."""
        return self._entry.data.get(CONF_BACKUP_PATH, "")

    @handle_backup_errors
    async def async_download_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        """Download a backup file.

        :param backup_id: The ID of the backup that was returned in async_list_backups.
        :return: An async iterator that yields bytes.
        """

        async def download_iterator():
            _LOGGER.info("Download backup %s on %s", backup_id, self._client)
            async with self._client.connect() as connection:
                backup = await self._find_backup_by_id(backup_id, connection)
                async with connection.download_stream(
                    f"{self._backup_path}/{suggested_filename(backup)}"
                ) as stream:
                    async for block in stream.iter_by_block():
                        yield block

        return download_iterator()

    @handle_backup_errors
    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        **kwargs: Any,
    ) -> None:
        """Upload a backup.

        :param open_stream: A function returning an async iterator that yields bytes.
        :param backup: Metadata about the backup that should be uploaded.
        """
        (filename_tar, filename_meta) = suggested_filenames(backup)

        _LOGGER.info("Upload backup on %s", self._client)
        async with self._client.connect() as connection:
            async with connection.upload_stream(f"{self._backup_path}/{filename_tar}") as stream:
                async for block in await open_stream():
                    await stream.write(block)

            _LOGGER.debug(
                "Uploaded backup to %s",
                f"{self._backup_path}/{filename_tar}",
            )

            async with connection.upload_stream(f"{self._backup_path}/{filename_meta}") as stream:
                await stream.write(json_dumps(backup.as_dict()).encode("utf8"))

            _LOGGER.debug(
                "Uploaded metadata file for %s",
                f"{self._backup_path}/{filename_meta}",
            )

        # reset cache
        self._cache_expiration = time()

    @handle_backup_errors
    async def async_delete_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> None:
        """Delete a backup file.

        :param backup_id: The ID of the backup that was returned in async_list_backups.
        """
        _LOGGER.info("Delete backup %s on %s", backup_id, self._client)
        async with self._client.connect() as connection:
            backup = await self._find_backup_by_id(backup_id, connection)

            (filename_tar, filename_meta) = suggested_filenames(backup)
            backup_path = f"{self._backup_path}/{filename_tar}"

            await connection.remove(backup_path)
            await connection.remove(f"{self._backup_path}/{filename_meta}")

        _LOGGER.debug(
            "Deleted backup at %s",
            backup_path,
        )

        # reset cache
        self._cache_expiration = time()

    @handle_backup_errors
    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """List backups."""
        _LOGGER.info("List existing backups on %s", self._client)
        return list((await self._list_cached_metadata_files()).values())

    @handle_backup_errors
    async def async_get_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AgentBackup:
        """Return a backup."""
        _LOGGER.info("Get backup %s on %s", backup_id, self._client)
        return await self._find_backup_by_id(backup_id)

    async def _list_cached_metadata_files(
        self, connection: FtpConnection = None
    ) -> dict[str, AgentBackup]:
        """List metadata files with a cache."""
        if time() <= self._cache_expiration:
            return self._cache_metadata_files

        if not connection:
            async with self._client.connect() as connection:
                return await self._list_cached_metadata_files(connection)

        async def _download_metadata(path: str) -> AgentBackup:
            """Download metadata file."""
            async with connection.download_stream(path) as stream:
                metadata = await stream.read()
            return AgentBackup.from_dict(json_loads_object(metadata.decode("utf8")))

        async def _list_metadata_files() -> dict[str, AgentBackup]:
            """List metadata files."""
            files = await connection.list(self._backup_path)
            return {
                metadata_content.backup_id: metadata_content
                for file_path, _ in files
                if str(file_path).endswith(".metadata.json")
                if (metadata_content := await _download_metadata(file_path))
            }

        self._cache_metadata_files = await _list_metadata_files()
        self._cache_expiration = time() + CACHE_TTL
        return self._cache_metadata_files

    async def _find_backup_by_id(
        self, backup_id: str, connection: FtpConnection = None
    ) -> AgentBackup:
        """Find a backup by its backup ID on remote."""
        metadata_files = await self._list_cached_metadata_files(connection)
        if metadata_file := metadata_files.get(backup_id):
            return metadata_file

        raise BackupNotFound(f"Backup {backup_id} not found")
