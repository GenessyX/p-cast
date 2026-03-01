import asyncio
import contextlib
import logging
import os
import signal
import socket
import tempfile
import typing
from collections.abc import AsyncIterator, Callable, Coroutine
from functools import partial
from typing import override
from uuid import UUID

from pychromecast.controllers.media import MediaStatus, MediaStatusListener
from pychromecast.error import RequestTimeout
from pychromecast.socket_client import (
    CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_LOST,
    ConnectionStatus,
    ConnectionStatusListener,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from p_cast.cast import CastDiscovery, get_local_ip, subscribe_to_stream
from p_cast.config import MAX_SAMPLE_RATE, StreamConfig
from p_cast.device import SinkController, SinkInputMonitor
from p_cast.ffmpeg import create_ffmpeg_stream_command

logger = logging.getLogger(__name__)


def _sd_notify(state: str) -> None:
    """Send a sd_notify(3) message if running under systemd (NOTIFY_SOCKET is set)."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        logger.info("NOTIFY_SOCKET not set, skipping sd_notify")

        return
    logger.info("Sending sd_notify(%s) to systemd", state)
    addr = "\0" + sock_path[1:] if sock_path.startswith("@") else sock_path
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(state.encode(), addr)


_FFMPEG_STARTUP_DELAY = 0.5
_STREAM_SUBSCRIBE_DELAY = 2
_BUFFERING_RECOVER_TIMEOUT = 6  # seconds of sustained BUFFERING before restarting stream (6 is minimum)


async def _tcp_probe(host: str, port: int, probe_timeout: float = 3.0) -> bool:
    """Check if a host is accepting TCP connections. No application-level traffic."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=probe_timeout,
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
        # client = typing.cast("tuple[str, str]", request.scope["client"])
        # method = typing.cast("str", request.scope["method"])
        # path = typing.cast("str", request.scope["path"])

        try:
            response: Response = await call_next(request)
        except FileNotFoundError:
            # Race: temp dir was cleaned up (stream teardown) while a segment
            # request was in-flight. Return 404 rather than crashing the server.
            return Response(status_code=404)

        # raw_length = response.headers.get("content-length")
        # content_length = int(raw_length) / 1024 if raw_length else 0

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


class BufferingWatchdog(MediaStatusListener):
    """Detects sustained BUFFERING and triggers a stream restart.

    new_media_status() is called from pychromecast's socket thread; all asyncio
    interactions are scheduled via call_soon_threadsafe onto the event loop.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        sink_name: str,
        on_recover: Callable[[str], Coroutine[None, None, None]],
    ) -> None:
        self._loop = loop
        self._sink_name = sink_name
        self._on_recover = on_recover
        self._active = True
        self._timer_task: asyncio.Task[None] | None = None

    @override
    def new_media_status(self, status: MediaStatus) -> None:
        if not self._active:
            return
        if status.player_state in "BUFFERING":
            self._loop.call_soon_threadsafe(self._start_timer)
        else:  # PAUSED, PLAYING, IDLE, UNKNOWN
            self._loop.call_soon_threadsafe(self._cancel_timer)

    @override
    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        logger.warning(
            "Media load failed for %s (queue_item=%d, error=%d)",
            self._sink_name,
            queue_item_id,
            error_code,  # see pychromecast.controllers.media.MEDIA_PLAYER_ERROR_CODES for interpretation
        )

    def _start_timer(self) -> None:
        """Schedule recovery if not already pending. Called on event loop."""
        if self._timer_task is None or self._timer_task.done():
            self._timer_task = asyncio.create_task(self._recover_after_timeout())
            logger.info("Buffering watchdog: starting recovery timer for %s", self._sink_name)

    def _cancel_timer(self) -> None:
        """Cancel a pending recovery timer. Called on event loop."""
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None
            logger.info("Buffering watchdog: cancelling recovery timer for %s", self._sink_name)

    async def _recover_after_timeout(self) -> None:
        try:
            await asyncio.sleep(_BUFFERING_RECOVER_TIMEOUT)
        except asyncio.CancelledError:
            return
        # Clear before recovery so a concurrent cancel() won't re-cancel a running task
        self._timer_task = None
        if not self._active:
            return
        logger.warning(
            "Buffering watchdog: %ds without recovery for %s, restarting stream",
            _BUFFERING_RECOVER_TIMEOUT,
            self._sink_name,
        )
        await self._on_recover(self._sink_name)

    def cancel(self) -> None:
        """Permanently disable watchdog. Called on event loop during stream teardown."""
        self._active = False
        self._cancel_timer()


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
        connection_listener: CastConnectionListener,
        buffering_watchdog: BufferingWatchdog,
    ) -> None:
        self.ffmpeg_process = ffmpeg_process
        self.stream_dir = stream_dir
        self.stream_app = stream_app
        self.subscribe_task = subscribe_task
        self.volume_controller = volume_controller
        self.connection_listener = connection_listener
        self.buffering_watchdog = buffering_watchdog

    async def teardown(self) -> None:
        self.connection_listener.deactivate()
        self.buffering_watchdog.cancel()
        self.subscribe_task.cancel()
        with contextlib.suppress(Exception):
            # tell it to stop, but don't quit() the CC app (and trigger a disconnect)
            self.volume_controller.cast.media_controller.stop(timeout=1.0)
        if self.ffmpeg_process.returncode is None:
            self.ffmpeg_process.terminate()
            await self.ffmpeg_process.wait()
        try:
            await self.volume_controller.stop_volume_sync()
        except Exception:
            logger.warning("Error stopping volume sync during teardown", exc_info=True)
        self.stream_dir.cleanup()


@contextlib.asynccontextmanager
async def lifespan(  # noqa: C901, PLR0915
    app: Starlette,
    controllers: dict[str, SinkController],
    stream_config: StreamConfig,
    discovery: CastDiscovery,
    streaming_port: int,
) -> AsyncIterator[None]:
    for controller in controllers.values():
        await controller.init()

    active_stream: ActiveStream | None = None
    local_ip = None  # defer initialization until we have devices (and thus a network up)
    loop = asyncio.get_running_loop()

    async def handle_cast_disconnect(sink_name: str) -> None:
        nonlocal active_stream
        controller = controllers[sink_name]
        # Make this idempotent: LOST can fire from the socket thread even while the
        # event loop is already processing a deactivation / device-remove.
        if not controller.available:
            return
        logger.warning("Chromecast disconnected, tearing down sink: %s", sink_name)
        await on_deactivate(sink_name)
        controller.available = False
        with contextlib.suppress(TimeoutError):
            controller.cast.disconnect(timeout=0.0)
        await controller.remove_sink()
        await monitor.refresh_sink_indices()
        monitor.clear_active()

    async def on_activate(sink_name: str) -> None:  # noqa: PLR0915, C901
        nonlocal active_stream
        nonlocal local_ip

        controller = controllers[sink_name]
        if not controller.available:
            logger.warning("Skipping activation for unavailable device: %s", sink_name)
            return

        if not local_ip:
            local_ip = get_local_ip()

        # Stop any leftover socket thread from the previous chromecast object.
        # No-op on first activation (socket thread not started until wait()).
        with contextlib.suppress(RuntimeError, TimeoutError):
            controller.cast.disconnect(timeout=0.0)
        cast = discovery.create_chromecast(controller.device_id)
        if cast is None:
            logger.warning("Device gone from zeroconf during activation: %s", sink_name)
            controller.available = False
            await controller.remove_sink()
            await monitor.refresh_sink_indices()
            monitor.clear_active()
            return
        controller.cast = cast
        with contextlib.suppress(RequestTimeout):
            await asyncio.to_thread(cast.wait, timeout=CAST_CONNECT_TIMEOUT)
            # (cast.status will be None upon timeout)

        if cast.status is None:
            logger.warning(
                "Chromecast not reachable within %ds upon activation, removing sink: %s",
                CAST_CONNECT_TIMEOUT,
                sink_name,
            )
            controller.available = False
            with contextlib.suppress(TimeoutError):
                cast.disconnect(timeout=0.0)
            await controller.remove_sink()
            await monitor.refresh_sink_indices()
            monitor.clear_active()
            return

        stream_dir = tempfile.TemporaryDirectory()
        ffmpeg_process: asyncio.subprocess.Process | None = None
        subscribe_task: asyncio.Task[None] | None = None

        try:
            sink_info = await controller.get_sink()
            sink = sink_info.name  # pyright: ignore[reportAttributeAccessIssue]
            logger.info("Activating cast from sink: %s", sink)

            # Generally don't resample pipewire streams; use the same rate pipewire uses (configurable in pipewire.conf)
            # Only resample if PipeWire's rate exceeds Chromecast's max (96kHz)
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

            # Give FFmpeg a moment to fail on startup errors (missing libs, bad args).
            # An immediate exit is a programming or configuration error — shut down.
            await asyncio.sleep(_FFMPEG_STARTUP_DELAY)
            if ffmpeg_process.returncode is not None:
                if _shutting_down:
                    # FFmpeg was killed by the SIGTERM handler; don't re-signal.
                    return
                stderr = await ffmpeg_process.stderr.read() if ffmpeg_process.stderr else b""
                logger.error(
                    "FFmpeg exited immediately (code %d): %s — shutting down",
                    ffmpeg_process.returncode,
                    stderr.decode(errors="replace").strip(),
                )
                with contextlib.suppress(TimeoutError):
                    cast.disconnect(timeout=0.0)
                stream_dir.cleanup()
                # trigger graceful shutdown handler
                os.kill(os.getpid(), signal.SIGTERM)
                return

            logger.debug("Publishing audio stream for chromecast")
            stream_app = StaticFilesWithCORS(StaticFiles(directory=stream_dir.name))
            app.mount("/stream", stream_app)

            buffering_watchdog = BufferingWatchdog(
                loop=loop,
                sink_name=sink_name,
                on_recover=restart_stream,
            )
            cast.media_controller.register_status_listener(buffering_watchdog)

            async def subscribe() -> None:
                await asyncio.sleep(_STREAM_SUBSCRIBE_DELAY)
                try:
                    subscribe_to_stream(cast.media_controller, local_ip, streaming_port, stream_config)
                except Exception:
                    logger.exception("Failed to subscribe Chromecast to stream")

            subscribe_task = asyncio.create_task(subscribe())

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
                connection_listener=connection_listener,
                buffering_watchdog=buffering_watchdog,
            )

            app.state.active_controller = controller

        except Exception:
            logger.exception("Failed to activate stream for sink: %s", sink_name)
            if subscribe_task is not None:
                subscribe_task.cancel()
            if ffmpeg_process is not None and ffmpeg_process.returncode is None:
                ffmpeg_process.terminate()
                await ffmpeg_process.wait()
            stream_dir.cleanup()

    async def on_deactivate(sink_name: str) -> None:
        nonlocal active_stream

        if active_stream is not None:
            logger.info("Deactivating cast from sink: %s", sink_name)
            await active_stream.teardown()
            app.routes[:] = [r for r in app.routes if not (hasattr(r, "path") and r.path == "/stream")]
            active_stream = None
            app.state.active_controller = None
            with contextlib.suppress(TimeoutError):
                controllers[sink_name].cast.disconnect(timeout=0.0)

    async def restart_stream(sink_name: str) -> None:
        await on_deactivate(sink_name)
        await on_activate(sink_name)

    monitor = SinkInputMonitor(
        controllers=controllers,
        on_activate=on_activate,
        on_deactivate=on_deactivate,
    )
    await monitor.start()

    async def mark_device_unavailable(sink_name: str, controller: SinkController) -> None:
        """Shared cleanup for device removal and health-check failure."""
        if not controller.available:
            return
        if active_stream is not None and monitor.active_sink == sink_name:
            await on_deactivate(sink_name)
            monitor.clear_active()
        controller.available = False
        with contextlib.suppress(RuntimeError, TimeoutError):  # expect timeout; ok if cast is unset
            controller.cast.disconnect(timeout=0.0)
        await controller.remove_sink()
        await monitor.refresh_sink_indices()
        logger.info("Device no longer available: %s (%s)", sink_name, controller.device_id)

    def get_controller(device_id: UUID) -> SinkController | None:
        for controller in controllers.values():
            if controller.device_id == device_id:
                return controller
        return None

    def address_matches(controller: SinkController, new_address: tuple[str, int]) -> bool:
        current_address = (controller.cast.cast_info.host, controller.cast.cast_info.port)
        return new_address == current_address

    _handling_devices: set[UUID] = set()

    async def handle_device_add(device_id: UUID) -> None:
        """Guard to prevent simultaneous adds of the same device"""
        if device_id in _handling_devices:
            logger.debug("Skipping concurrent device_add of %s", device_id)
            return
        _handling_devices.add(device_id)
        try:
            await _handle_device_add(device_id)
        finally:
            _handling_devices.discard(device_id)

    async def _handle_device_add(device_id: UUID) -> None:
        """React to mDNS add_service or update_service events to recover previously unavailable devices,
        mark newly unreachable devices unavailable, and handle potential IP changes."""
        controller = get_controller(device_id)
        if controller:
            new_address = discovery.get_device_address(device_id)
            if new_address is None:
                return
            if controller.available and not address_matches(controller, new_address):
                logger.info(
                    "Device %s address changed to: %s:%d, re-adding",
                    controller.sink_name,
                    *new_address,
                )
                await mark_device_unavailable(controller.sink_name, controller)
            elif not controller.available and not await _tcp_probe(*new_address):
                # Cheap pre-filter: skip expensive wait() if TCP is dead
                logger.debug("TCP probe failed for %s at %s:%d", controller.sink_name, *new_address)
                return

        # Before (re-)adding, ensure we can actually connect at the application level

        # NOTE: pychromecast's HostBrowser polls devices every 30s via HTTP.
        # After 5 consecutive failures (~150s) it fires remove_service,
        # but if the device still responds to mDNS (network stack up,
        # chromecast app hung) zeroconf re-adds it within seconds —
        # suppressing our remove_cast callback entirely. A device can
        # stay in this state for hours (until device mDNS truly fails).
        # (see pychromecast #1168)
        #
        # For available devices, this check catches that case. For
        # normally-functioning chromecasts this only runs on zeroconf
        # TTL cycles (~75 min) and completes quickly (1-3s).
        # For unavailable devices, this verifies genuine availability.
        chromecast = discovery.create_chromecast(device_id)
        if chromecast is None:
            return
        with contextlib.suppress(RequestTimeout):
            await asyncio.to_thread(chromecast.wait, timeout=CAST_CONNECT_TIMEOUT)
        with contextlib.suppress(TimeoutError):
            chromecast.disconnect(timeout=0.0)

        if chromecast.status is None:
            if controller and controller.available:
                logger.warning("Device %s (%s) failed health check on add/update", controller.sink_name, device_id)
                await mark_device_unavailable(controller.sink_name, controller)
            else:
                logger.debug("Device %s not yet reachable at app level", device_id)
            return

        if not controller:
            controller = SinkController(chromecast=chromecast)
            await controller.init()
            controllers[controller.sink_name] = controller
            logger.info("Device added: %s", controller.sink_name)
        elif not controller.available:
            controller.available = True
            await controller.init()
            logger.info("Device restored: %s", controller.sink_name)
        await monitor.refresh_sink_indices()

    async def handle_device_remove(device_id: UUID) -> None:
        for sink_name, controller in controllers.items():
            if controller.device_id == device_id:
                await mark_device_unavailable(sink_name, controller)
                return

    discovery.set_callbacks(
        on_add=lambda uuid: asyncio.run_coroutine_threadsafe(handle_device_add(uuid), loop),  # type: ignore[arg-type]
        on_remove=lambda uuid: asyncio.run_coroutine_threadsafe(handle_device_remove(uuid), loop),  # type: ignore[arg-type]
        on_update=lambda uuid: asyncio.run_coroutine_threadsafe(handle_device_add(uuid), loop),  # type: ignore[arg-type]
    )

    app.state.controllers = controllers
    app.state.monitor = monitor
    app.state.active_controller = None

    _shutting_down = False

    async def _sigterm_handler() -> None:
        nonlocal active_stream, _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        logger.info("SIGTERM received, running cleanup")
        # Claim active_stream so the post-yield path skips it if both paths run.
        _stream, active_stream = active_stream, None
        with contextlib.suppress(Exception):
            if _stream is not None:
                await asyncio.wait_for(_stream.teardown(), timeout=5.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(monitor.stop(), timeout=3.0)
        for ctrl in list(controllers.values()):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ctrl.close(), timeout=3.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.to_thread(discovery.stop), timeout=3.0)
        os._exit(0)

    # Granian's Rust/Tokio layer seems to block SIGTERM process-wide for signalfd use.
    # Unblocking it in the event-loop thread and registering an asyncio handler
    # gives us a reliable shutdown path that bypasses any signalfd issues.
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_sigterm_handler()))
    except Exception:
        logger.debug("Could not install SIGTERM handler in event loop", exc_info=True)

    # notify systemd that we are up (if applicable)
    _sd_notify("READY=1")
    yield

    if active_stream is not None:
        await active_stream.teardown()
    await monitor.stop()
    for controller in controllers.values():
        await controller.close()
    discovery.stop()


def _get_active_media_controller(request: Request) -> typing.Any:  # noqa: ANN401
    controller: SinkController | None = request.app.state.active_controller  # pyright: ignore[reportAny]
    if controller is None:
        return None
    return controller.cast.media_controller


async def pause(request: Request) -> Response:
    media_controller = _get_active_media_controller(request)
    if media_controller is None:
        return Response(content="No active device", status_code=404)
    media_controller.pause()
    return Response(content="OK")


async def play(request: Request) -> Response:
    media_controller = _get_active_media_controller(request)
    if media_controller is None:
        return Response(content="No active device", status_code=404)
    media_controller.play()
    media_controller.seek(None)  # pyright: ignore[reportArgumentType]
    return Response(content="OK")


async def devices(request: Request) -> Response:
    controllers: dict[str, SinkController] = request.app.state.controllers  # pyright: ignore[reportAny]
    monitor: SinkInputMonitor = request.app.state.monitor  # pyright: ignore[reportAny]
    active_sink = monitor.active_sink

    device_list = [
        {
            "sink_name": sink_name,
            "friendly_name": controller.cast.name,
            "active": sink_name == active_sink,
            "available": controller.available,
        }
        for sink_name, controller in controllers.items()
    ]
    return JSONResponse(device_list)


def create_app() -> Starlette:
    # should all be set in main()
    # assert all(v in os.environ for v in ("PCAST_LOG_LEVEL", "PCAST_PORT",
    # "PCAST_BITRATE", "PCAST_FFMPEG"))

    log_level = getattr(logging, os.environ["PCAST_LOG_LEVEL"], logging.INFO)
    if "PCAST_LOG_FILE" in os.environ:
        logging.basicConfig(
            filename=os.environ["PCAST_LOG_FILE"],
            level=log_level,
            format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
        )
    else:
        logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")

    logger.info("\n\n*** p-cast instance starting ***\n")

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
        controllers[controller.sink_name] = controller

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
