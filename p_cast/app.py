import asyncio
import contextlib
import logging
import os
import tempfile
import typing
from collections.abc import AsyncIterator, Callable, Coroutine
from functools import partial
from typing import override
from uuid import UUID

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from pychromecast.error import RequestTimeout
from pychromecast.socket_client import (
    ConnectionStatus,
    ConnectionStatusListener,
    CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_LOST,
)

from p_cast.cast import CastDiscovery, get_local_ip, subscribe_to_stream
from p_cast.config import StreamConfig
from p_cast.device import SinkController, SinkInputMonitor
from p_cast.ffmpeg import create_ffmpeg_stream_command

logger = logging.getLogger(__name__)


async def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    """Check if a host is accepting TCP connections. No application-level traffic."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
    except (OSError, TimeoutError):
        return False
    return True


class StaticFilesWithCORS(BaseHTTPMiddleware):
    """Wraps StaticFiles to add CORS headers and disable caching on HLS segment responses."""
    @override
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        client = typing.cast("tuple[str, str]", request.scope["client"])
        method = typing.cast("str", request.scope["method"])
        path = typing.cast("str", request.scope["path"])

        response: Response = await call_next(request)

        raw_length = response.headers.get("content-length")
        content_length = int(raw_length) / 1024 if raw_length else 0

        # logger.debug(
        #     "[%s:%s] %s - %s - %s KiB",
        #     client[0],
        #     client[1],
        #     method,
        #     path,
        #     content_length,
        # )
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Cache-Control"] = "no-cache"

        for header in ("etag", "accept-ranges"):
            if header in response.headers:
                del response.headers[header]
        return response


class CastConnectionListener(ConnectionStatusListener):
    """Detects Chromecast connection loss and triggers stream teardown."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        sink_name: str,
        on_disconnect: Callable[[str], Coroutine[None, None, None]],
    ) -> None:
        self._loop = loop
        self._sink_name = sink_name
        self._on_disconnect = on_disconnect
        self._active = True

    def deactivate(self) -> None:
        self._active = False

    @override
    def new_connection_status(self, status: ConnectionStatus) -> None:
        if not self._active:
            return
        if status.status in (CONNECTION_STATUS_LOST, CONNECTION_STATUS_DISCONNECTED):
            logger.warning("Chromecast connection %s: %s", status.status, self._sink_name)
            asyncio.run_coroutine_threadsafe(
                self._on_disconnect(self._sink_name),
                self._loop,
            )


CAST_CONNECT_TIMEOUT = 10  # seconds to wait for Chromecast connection before giving up


class ActiveStream:
    """Holds all resources for a running stream (FFmpeg process, temp dir, Starlette mount).

    Torn down as a unit when the sink is deactivated or the app shuts down.
    """

    def __init__(
        self,
        ffmpeg_process: asyncio.subprocess.Process,
        stream_dir: tempfile.TemporaryDirectory[str],
        stream_app: StaticFilesWithCORS,
        subscribe_task: asyncio.Task[None],
        volume_controller: SinkController,
        ffmpeg_watcher: asyncio.Task[None],
        connection_listener: CastConnectionListener,
    ) -> None:
        self.ffmpeg_process = ffmpeg_process
        self.stream_dir = stream_dir
        self.stream_app = stream_app
        self.subscribe_task = subscribe_task
        self.volume_controller = volume_controller
        self.ffmpeg_watcher = ffmpeg_watcher
        self.connection_listener = connection_listener

    async def teardown(self) -> None:
        self.connection_listener.deactivate()
        self.subscribe_task.cancel()
        self.ffmpeg_watcher.cancel()
        if self.ffmpeg_process.returncode is None:
            self.ffmpeg_process.terminate()
            await self.ffmpeg_process.wait()
        try:
            await self.volume_controller.stop_volume_sync()
        except Exception:
            logger.warning("Error stopping volume sync during teardown", exc_info=True)
        self.stream_dir.cleanup()


@contextlib.asynccontextmanager
async def lifespan(
    app: Starlette,
    controllers: dict[str, SinkController],
    stream_config: StreamConfig,
    discovery: CastDiscovery,
    streaming_port: int,
) -> AsyncIterator[None]:
    for controller in controllers.values():
        await controller.init()

    active_stream: ActiveStream | None = None
    local_ip = get_local_ip()
    loop = asyncio.get_running_loop()

    async def handle_cast_disconnect(sink_name: str) -> None:
        nonlocal active_stream
        controller = controllers[sink_name]
        # Make this idempotent: LOST can fire from the socket thread even while the
        # event loop is already processing a deactivation / device-remove.
        if not controller.available:
            return
        logger.warning("Chromecast disconnected, tearing down: %s", sink_name)
        await on_deactivate(sink_name)
        controller.available = False
        controller._cast.disconnect()
        await controller.remove_sink()
        await monitor.refresh_sink_indices()
        monitor.clear_active()

    async def on_activate(sink_name: str) -> None:
        nonlocal active_stream

        controller = controllers[sink_name]
        if not controller.available:
            logger.warning("Skipping activation for unavailable device: %s", sink_name)
            return

        # Stop any leftover socket thread from the previous chromecast object.
        # No-op on first activation (socket thread not started until wait()).
        try:
            controller._cast.disconnect()
        except RuntimeError:
            pass  # that should be fine
        cast = discovery.create_chromecast(controller._cast.uuid)
        if cast is None:
            logger.warning("Device gone from zeroconf during activation: %s", sink_name)
            controller.available = False
            await controller.remove_sink()
            await monitor.refresh_sink_indices()
            monitor.clear_active()
            return
        controller._cast = cast
        try:
            await asyncio.to_thread(cast.wait, timeout=CAST_CONNECT_TIMEOUT)
        except RequestTimeout:
            pass  # cast.status will be None

        if cast.status is None:
            logger.warning(
                "Chromecast not reachable within %ds upon activation, removing sink: %s",
                CAST_CONNECT_TIMEOUT,
                sink_name,
            )
            controller.available = False
            cast.disconnect()
            await controller.remove_sink()
            await monitor.refresh_sink_indices()
            monitor.clear_active()
            return

        stream_dir = tempfile.TemporaryDirectory()  # noqa: SIM115
        ffmpeg_process: asyncio.subprocess.Process | None = None
        subscribe_task: asyncio.Task[None] | None = None
        ffmpeg_watcher: asyncio.Task[None] | None = None

        try:
            sink_info = await controller.get_sink()
            sink = sink_info.name  # pyright: ignore[reportAttributeAccessIssue]
            logger.info("Activating cast from sink: %s", sink)

            # Generally don't resample pipewire streams; use the same rate pipewire uses (configurable in pipewire.conf)
            # Only resample if PipeWire's rate exceeds Chromecast's max (96kHz)
            from p_cast.config import MAX_SAMPLE_RATE
            pw_rate: int = sink_info.sample_spec.rate  # pyright: ignore[reportAttributeAccessIssue, reportAssignmentType]
            sample_rate = min(pw_rate, MAX_SAMPLE_RATE) if pw_rate > MAX_SAMPLE_RATE else None

            ffmpeg_command = create_ffmpeg_stream_command(
                sink=f"{sink}.monitor",
                stream_dir=stream_dir.name,
                sample_rate=sample_rate,
                config=stream_config,
            )
            logger.info("Starting ffmpeg: %s", " ".join(ffmpeg_command))

            ffmpeg_process = await asyncio.create_subprocess_exec(
                *ffmpeg_command,
                stderr=asyncio.subprocess.PIPE,
            )

            # Give FFmpeg a moment to fail on startup errors (missing libs, bad args)
            await asyncio.sleep(0.5)
            if ffmpeg_process.returncode is not None:
                stderr = await ffmpeg_process.stderr.read() if ffmpeg_process.stderr else b""
                logger.error(
                    "FFmpeg exited immediately (code %d): %s",
                    ffmpeg_process.returncode,
                    stderr.decode(errors="replace").strip(),
                )
                stream_dir.cleanup()
                return

            logger.debug("Publishing audio stream for chromecast")
            stream_app = StaticFilesWithCORS(StaticFiles(directory=stream_dir.name))
            app.mount("/stream", stream_app)

            async def subscribe() -> None:
                await asyncio.sleep(2)
                try:
                    subscribe_to_stream(cast.media_controller, local_ip, streaming_port, stream_config)
                except Exception:
                    logger.exception("Failed to subscribe Chromecast to stream")

            subscribe_task = asyncio.create_task(subscribe())

            async def watch_ffmpeg() -> None:
                assert ffmpeg_process is not None
                await ffmpeg_process.wait()
                if active_stream is not None and active_stream.ffmpeg_process is ffmpeg_process:
                    stderr_data = b""
                    if ffmpeg_process.stderr:
                        stderr_data = await ffmpeg_process.stderr.read()
                    logger.error(
                        "FFmpeg exited unexpectedly (code %d): %s",
                        ffmpeg_process.returncode,
                        stderr_data.decode(errors="replace").strip(),
                    )
                    # in this case, audio is still routed to the sink but not sent to chromecast. The user will notice silence and can switch from/back to the sink to retry.
                    await on_deactivate(sink_name)
                    # Reset monitor state so re-detection can also happen on the
                    # next PulseAudio event (e.g. new sink-input or volume change)
                    monitor.clear_active()


            ffmpeg_watcher = asyncio.create_task(watch_ffmpeg())

            await controller.start_volume_sync()

            connection_listener = CastConnectionListener(
                loop=loop,
                sink_name=sink_name,
                on_disconnect=handle_cast_disconnect,
            )
            cast.register_connection_listener(connection_listener)

            active_stream = ActiveStream(
                ffmpeg_process=ffmpeg_process,
                stream_dir=stream_dir,
                stream_app=stream_app,
                subscribe_task=subscribe_task,
                volume_controller=controller,
                ffmpeg_watcher=ffmpeg_watcher,
                connection_listener=connection_listener,
            )

            app.state.active_controller = controller

        except Exception:
            logger.exception("Failed to activate stream for sink: %s", sink_name)
            if subscribe_task is not None:
                subscribe_task.cancel()
            if ffmpeg_watcher is not None:
                ffmpeg_watcher.cancel()
            if ffmpeg_process is not None and ffmpeg_process.returncode is None:
                ffmpeg_process.terminate()
                await ffmpeg_process.wait()
            stream_dir.cleanup()

    async def on_deactivate(sink_name: str) -> None:
        nonlocal active_stream

        if active_stream is not None:
            logger.info("Deactivating cast from sink: %s", sink_name)
            await active_stream.teardown()
            app.routes[:] = [
                r for r in app.routes if not (hasattr(r, "path") and r.path == "/stream")  # type: ignore[union-attr]
            ]
            active_stream = None
            app.state.active_controller = None
            controllers[sink_name]._cast.disconnect()

    monitor = SinkInputMonitor(
        controllers=controllers,
        on_activate=on_activate,
        on_deactivate=on_deactivate,
    )
    await monitor.start()

    async def handle_device_add(device_id: UUID) -> None:
        # Check if controller already exists (device reappearing)
        for sink_name, controller in controllers.items():
            if controller._cast.uuid == device_id:
                if controller.available:
                    return
                # Was unavailable (connection timed out) — TCP probe before restoring
                address = discovery.get_device_address(device_id)
                if address is None:
                    return
                if not await _tcp_probe(*address):
                    logger.debug("TCP probe failed for %s at %s:%d", sink_name, *address)
                    return
                # Device is reachable again — verify at the application level
                # (failing devices can connect with TCP but fail at TLS/chromecast level)
                chromecast = discovery.create_chromecast(device_id)
                if chromecast is None:
                    return
                try:
                    await asyncio.to_thread(chromecast.wait, timeout=CAST_CONNECT_TIMEOUT)
                except RequestTimeout:
                    pass
                chromecast.disconnect()
                if chromecast.status is None:
                    logger.debug("Device %s passed TCP probe but not reachable at app level, ignoring", sink_name)
                    return
                controller._cast = chromecast
                controller.available = True
                await controller.init()
                await monitor.refresh_sink_indices()
                logger.info("Device restored: %s", sink_name)
                return

        chromecast = discovery.create_chromecast(device_id)
        if chromecast is None:
            return
        controller = SinkController(chromecast=chromecast)
        await controller.init()
        controllers[controller._sink_name] = controller
        await monitor.refresh_sink_indices()

    async def handle_device_remove(device_id: UUID) -> None:
        for sink_name, controller in controllers.items():
            if controller._cast.uuid == device_id:
                if not controller.available:
                    return
                if active_stream is not None and monitor.active_sink == sink_name:
                    await on_deactivate(sink_name)
                    monitor.clear_active()
                controller.available = False
                try:
                    controller._cast.disconnect()
                except RuntimeError:
                    pass  # socket thread was never started (device was idle)
                await controller.remove_sink()
                await monitor.refresh_sink_indices()
                logger.info("Device unavailable: %s", sink_name)
                return

    discovery.set_callbacks(
        on_add=lambda uuid: asyncio.run_coroutine_threadsafe(handle_device_add(uuid), loop),
        on_remove=lambda uuid: asyncio.run_coroutine_threadsafe(handle_device_remove(uuid), loop),
    )

    app.state.controllers = controllers
    app.state.monitor = monitor
    app.state.active_controller = None

    yield

    if active_stream is not None:
        await active_stream.teardown()
    await monitor.stop()
    for controller in controllers.values():
        await controller.close()
    discovery.stop()


def get_active_media_controller(request: Request):  # type: ignore[no-untyped-def]  # noqa: ANN201
    controller: SinkController | None = request.app.state.active_controller  # pyright: ignore[reportAny]
    if controller is None:
        return None
    return controller._cast.media_controller


async def pause(request: Request) -> Response:
    media_controller = get_active_media_controller(request)
    if media_controller is None:
        return Response(content="No active device", status_code=404)
    media_controller.pause()
    return Response(content="OK")


async def play(request: Request) -> Response:
    media_controller = get_active_media_controller(request)
    if media_controller is None:
        return Response(content="No active device", status_code=404)
    media_controller.play()
    media_controller.seek(None)  # type: ignore[arg-type] # pyright: ignore[reportArgumentType]
    return Response(content="OK")


async def devices(request: Request) -> Response:
    controllers: dict[str, SinkController] = request.app.state.controllers  # pyright: ignore[reportAny]
    monitor: SinkInputMonitor = request.app.state.monitor  # pyright: ignore[reportAny]
    active_sink = monitor.active_sink

    device_list = [
        {
            "sink_name": sink_name,
            "friendly_name": controller._cast.name,
            "active": sink_name == active_sink,
            "available": controller.available,
        }
        for sink_name, controller in controllers.items()
    ]
    return JSONResponse(device_list)


def create_app() -> Starlette:
    # should all be set in main()
    assert all(v in os.environ for v in ("PCAST_LOG_LEVEL", "PCAST_PORT", "PCAST_BITRATE", "PCAST_FFMPEG"))

    log_level = getattr(logging, os.environ["PCAST_LOG_LEVEL"], logging.INFO)
    if "PCAST_LOG_FILE" in os.environ:
        logging.basicConfig(filename = os.environ["PCAST_LOG_FILE"], level=log_level, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    else:
        logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")

    logging.info("*** p-cast instance starting ***")

    discovery = CastDiscovery()
    chromecasts = discovery.discover()

    streaming_port = int(os.environ["PCAST_PORT"])
    stream_config = StreamConfig(
        acodec="aac",
        bitrate=os.environ["PCAST_BITRATE"],
        ffmpeg_bin=os.environ["PCAST_FFMPEG"],
    )

    controllers: dict[str, SinkController] = {}
    for chromecast in chromecasts:
        controller = SinkController(
            chromecast=chromecast,
        )
        controllers[controller._sink_name] = controller

    middleware = [
        Middleware(
            GZipMiddleware,
            minimum_size=1,
            compresslevel=9,
        ),
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_headers=["*"],
            allow_credentials=True,
            allow_methods=["*"],
        ),
    ]

    return Starlette(
        routes=[
            Route("/pause", endpoint=pause),
            Route("/play", endpoint=play),
            Route("/devices", endpoint=devices),
        ],
        lifespan=partial(
            lifespan,
            controllers=controllers,
            stream_config=stream_config,
            discovery=discovery,
            streaming_port=streaming_port,
        ),
        middleware=middleware,
    )
