import logging
import platform
import socket
import sys
import time
import typing
from collections.abc import Callable
from uuid import UUID

import pychromecast
import zeroconf
from pychromecast.controllers.media import (
    STREAM_TYPE_LIVE,
    MediaController,
)
from pychromecast.discovery import CastBrowser, SimpleCastListener

from p_cast.config import StreamConfig

logger = logging.getLogger(__name__)


class CastDiscovery:
    """Manages Chromecast discovery with persistent add/remove callbacks."""

    def __init__(self) -> None:
        self._zconf = zeroconf.Zeroconf()
        self._browser: CastBrowser | None = None
        self._on_add: Callable[[UUID], None] | None = None
        self._on_remove: Callable[[UUID], None] | None = None
        self._on_update: Callable[[UUID], None] | None = None

    def discover(self) -> list[pychromecast.Chromecast]:  # noqa: C901
        """Block for initial discovery, return found Chromecasts."""
        initial_uuids: set[UUID] = set()
        initial_phase = True

        def add_callback(device_id: UUID, service: str) -> None:
            nonlocal initial_phase
            logger.info("[%s] added: %s", device_id, service)
            if initial_phase:
                initial_uuids.add(device_id)
            elif self._on_add is not None:
                self._on_add(device_id)

        def remove_callback(device_id: UUID, service: str, cast_info: object) -> None:  # noqa: ARG001
            logger.info("[%s] removed: %s", device_id, service)
            if self._on_remove is not None:
                self._on_remove(device_id)

        def update_callback(device_id: UUID, service: str) -> None:
            logger.info("[%s] updated: %s", device_id, service)
            if initial_phase:
                initial_uuids.add(device_id)
            elif self._on_update is not None:
                self._on_update(device_id)

        self._browser = CastBrowser(
            SimpleCastListener(
                add_callback=add_callback,
                remove_callback=remove_callback,
                update_callback=update_callback,
            ),
            self._zconf,
        )
        self._browser.start_discovery()
        time.sleep(2)
        initial_phase = False

        if not initial_uuids:
            logger.error("No Chromecasts found")
            sys.exit(1)

        chromecasts: list[pychromecast.Chromecast] = []
        for uuid in initial_uuids:
            cast_info = self._browser.devices.get(uuid)
            if cast_info is None:
                logger.warning("Device %s disappeared during startup", uuid)
                continue
            cc = pychromecast.get_chromecast_from_cast_info(cast_info, self._zconf)
            chromecasts.append(cc)

        if not chromecasts:
            logger.error("No Chromecasts could be connected")
            sys.exit(1)

        return chromecasts

    def set_callbacks(
        self,
        on_add: Callable[[UUID], None],
        on_remove: Callable[[UUID], None],
        on_update: Callable[[UUID], None] | None = None,
    ) -> None:
        """Register callbacks for devices appearing/disappearing/updating after initial discovery."""
        self._on_add = on_add
        self._on_remove = on_remove
        self._on_update = on_update

    def get_device_address(self, device_id: UUID) -> tuple[str, int] | None:
        """Return (host, port) for a discovered device, or None if unknown."""
        if self._browser is None:
            return None
        cast_info = self._browser.devices.get(device_id)
        if cast_info is None or cast_info.host is None or cast_info.port is None:
            return None
        return (cast_info.host, cast_info.port)

    def create_chromecast(self, device_id: UUID) -> pychromecast.Chromecast | None:
        """Create a Chromecast object for a dynamically discovered device."""
        if self._browser is None:
            return None
        cast_info = self._browser.devices.get(device_id)
        if cast_info is None:
            return None
        return pychromecast.get_chromecast_from_cast_info(cast_info, self._zconf)

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.stop_discovery()


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return typing.cast("str", s.getsockname()[0])
    finally:
        s.close()


def subscribe_to_stream(
    mc: MediaController,
    local_ip: str,
    streaming_port: int,
    config: StreamConfig,
) -> None:
    url = f"http://{local_ip}:{streaming_port}/stream/index.m3u8"
    logger.info("Subscribing to: %s", url)
    mc.play_media(
        url,
        content_type=config.content_type,
        title=f"p-cast stream from {platform.node()}",
        stream_type=STREAM_TYPE_LIVE,
        media_info={
            "hlsSegmentFormat": config.chromecast_hls_segment_type,
        },
    )
