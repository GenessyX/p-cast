import asyncio
import contextlib
import logging
import re
import tempfile
import typing
from collections.abc import AsyncIterator
from functools import partial
from typing import override

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from pychromecast.discovery import CastBrowser

from p_cast.cast import find_chromecasts, get_local_ip, subscribe_to_stream
from p_cast.config import StreamConfig
from p_cast.device import SinkController, SinkInputMonitor
from p_cast.ffmpeg import create_ffmpeg_stream_command

logger = logging.getLogger(__name__)

_SINK_NAME_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _make_sink_name(friendly_name: str) -> str:
    sanitized = _SINK_NAME_INVALID_CHARS.sub("_", friendly_name)
    return f"{sanitized}_Cast"


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

        content_length = int(response.headers["Content-Length"]) / 1024

        logger.debug(
            "[%s:%s] %s - %s - %s KiB",
            client[0],
            client[1],
            method,
            path,
            content_length,
        )
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Cache-Control"] = "no-cache"

        del response.headers["etag"]
        del response.headers["accept-ranges"]
        return response


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
    ) -> None:
        self.ffmpeg_process = ffmpeg_process
        self.stream_dir = stream_dir
        self.stream_app = stream_app
        self.subscribe_task = subscribe_task
        self.volume_controller = volume_controller

    async def teardown(self) -> None:
        self.subscribe_task.cancel()
        if self.ffmpeg_process.returncode is None:
            self.ffmpeg_process.terminate()
            await self.ffmpeg_process.wait()
        await self.volume_controller.stop_volume_sync()
        self.stream_dir.cleanup()


@contextlib.asynccontextmanager
async def lifespan(
    app: Starlette,
    controllers: dict[str, SinkController],
    stream_config: StreamConfig,
    browser: CastBrowser,
) -> AsyncIterator[None]:
    for controller in controllers.values():
        await controller.init()

    active_stream: ActiveStream | None = None
    local_ip = get_local_ip()

    async def on_activate(sink_name: str) -> None:
        nonlocal active_stream

        controller = controllers[sink_name]
        cast = controller._cast
        cast.wait()

        stream_dir = tempfile.TemporaryDirectory()  # noqa: SIM115

        sink = await controller.get_sink_name()
        logger.info("Activating cast from sink: %s", sink)

        ffmpeg_command = create_ffmpeg_stream_command(
            sink=f"{sink}.monitor",
            stream_dir=stream_dir.name,
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

        stream_app = StaticFilesWithCORS(StaticFiles(directory=stream_dir.name))
        app.mount("/stream", stream_app)

        async def subscribe() -> None:
            await asyncio.sleep(2)
            subscribe_to_stream(cast.media_controller, local_ip, stream_config)

        subscribe_task = asyncio.create_task(subscribe())

        await controller.start_volume_sync()

        active_stream = ActiveStream(
            ffmpeg_process=ffmpeg_process,
            stream_dir=stream_dir,
            stream_app=stream_app,
            subscribe_task=subscribe_task,
            volume_controller=controller,
        )

        app.state.active_controller = controller

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

    monitor = SinkInputMonitor(
        controllers=controllers,
        on_activate=on_activate,
        on_deactivate=on_deactivate,
    )
    await monitor.start()

    app.state.controllers = controllers
    app.state.monitor = monitor
    app.state.active_controller = None

    yield

    if active_stream is not None:
        await active_stream.teardown()
    await monitor.stop()
    for controller in controllers.values():
        await controller.close()
    browser.stop_discovery()


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
        }
        for sink_name, controller in controllers.items()
    ]
    return JSONResponse(device_list)


def create_app() -> Starlette:
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger(__name__).setLevel(logging.INFO)

    chromecasts, browser = find_chromecasts()

    stream_config = StreamConfig(acodec="aac", bitrate="256k")

    controllers: dict[str, SinkController] = {}
    for cast in chromecasts:
        sink_name = _make_sink_name(cast.name)
        controllers[sink_name] = SinkController(
            chromecast=cast,
            sink_name=sink_name,
        )
        logger.info("Registered device: %s -> sink %s", cast.name, sink_name)

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
            browser=browser,
        ),
        middleware=middleware,
    )
