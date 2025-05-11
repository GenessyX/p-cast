import logging
import socket
import sys
import time
import typing
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


def find_chromecast() -> pychromecast.Chromecast:
    services: dict[UUID, str] = {}
    zconf = zeroconf.Zeroconf()

    def add_callback(device_id: UUID, service: str) -> None:
        logger.info("[%s] added: %s", device_id, service)
        services[device_id] = service

    browser = CastBrowser(
        SimpleCastListener(
            add_callback=add_callback,
        ),
        zconf,
    )
    browser.start_discovery()
    time.sleep(2)

    chromecasts, browser = pychromecast.get_listed_chromecasts(
        uuids=list(services.keys()),
    )
    if len(chromecasts) == 0:
        sys.exit(1)

    cast = chromecasts[0]
    cast.wait()
    pychromecast.discovery.stop_discovery(browser)
    return cast


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
    config: StreamConfig,
) -> None:
    url = f"http://{local_ip}:3000/stream/index.m3u8"
    logger.info("Subscribing to: %s", url)
    mc.play_media(
        url,
        content_type="application/vnd.apple.mpegurl",
        title="p-cast stream",
        stream_type=STREAM_TYPE_LIVE,
        media_info={
            "hlsSegmentFormat": config.chromecast_hls_segment_type,
        },
    )
    mc.seek(999999999)
