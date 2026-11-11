"""Active TV input via one long-lived ADB shell (push, ~1 s latency).

The TV runs a small loop that prints the active source every second:
`hdmiN` / tuner source name while the TV input player is on screen, `app` otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

from adb_shell.adb_device_async import AdbDeviceTcpAsync
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner

from .const import ADB_PORT

_LOGGER = logging.getLogger(__name__)

_LOOP = (
    "while true; do "
    "a=$(dumpsys activity activities 2>/dev/null | grep topResumedActivity); "
    'case "$a" in *PlayTvActivity*) getprop persist.odm.tpv.sys.lastsource;; *) echo app;; esac; '
    "sleep 1; done"
)


def _load_signer(key_path: str) -> PythonRSASigner:
    if not os.path.isfile(key_path):
        keygen(key_path)
    with open(key_path) as priv, open(key_path + ".pub") as pub:
        return PythonRSASigner(pub.read(), priv.read())


class AdbSource:
    def __init__(self, hass, host: str, key_path: str, on_source: Callable[[str | None], None]):
        self.hass = hass
        self.host = host
        self.key_path = key_path
        self._on_source = on_source
        self._task: asyncio.Task | None = None
        self.source: str | None = None

    def start(self) -> None:
        self._task = self.hass.async_create_background_task(self._run(), "halo_sync_adb")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _set(self, value: str | None) -> None:
        if value != self.source:
            self.source = value
            self._on_source(value)

    async def _run(self) -> None:
        signer = await self.hass.async_add_executor_job(_load_signer, self.key_path)
        while True:
            device = AdbDeviceTcpAsync(self.host, ADB_PORT, default_transport_timeout_s=10)
            try:
                # First connection shows "Allow USB debugging?" on the TV.
                await device.connect(rsa_keys=[signer], auth_timeout_s=60)
                async for chunk in device.streaming_shell(_LOOP, read_timeout_s=15):
                    for line in chunk.splitlines():
                        line = line.strip()
                        if line:
                            self._set(line)
            except asyncio.CancelledError:
                await device.close()
                raise
            except Exception as exc:  # noqa: BLE001 - TV off/standby/network: retry
                _LOGGER.debug("ADB source session ended: %s", exc)
            await device.close()
            self._set(None)
            await asyncio.sleep(5)
