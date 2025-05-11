import asyncio
import contextlib
import logging
import tempfile
import typing
from collections.abc import AsyncIterator
from functools import partial
from typing import override

from pychromecast import Chromecast
from pychromecast.controllers.media import MediaController
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from p_cast.cast import find_chromecast, get_local_ip, subscribe_to_stream
from p_cast.config import StreamConfig
from p_cast.device import SinkController
from p_cast.ffmpeg import create_ffmpeg_stream_command

logger = logging.getLogger(__name__)


class StaticFilesWithCORS(BaseHTTPMiddleware):
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


@contextlib.asynccontextmanager
async def lifespan(
    app: Starlette,
    chromecast: Chromecast,
) -> AsyncIterator[None]:
    sink_controller = SinkController(chromecast=chromecast)
    await sink_controller.init()

    stream_config = StreamConfig(acodec="aac", bitrate="256k")

    app.state.media_controller = chromecast

    async def subscribe(mc: MediaController, local_ip: str) -> None:
        await asyncio.sleep(2)
        subscribe_to_stream(mc, local_ip, stream_config)

    with tempfile.TemporaryDirectory() as stream_dir:
        sink = await sink_controller.get_sink_name()
        logger.info("Casting from sink: %s", sink)
        ffmpeg_command = create_ffmpeg_stream_command(
            sink=f"{sink}.monitor",
            stream_dir=stream_dir,
            config=stream_config,
        )
        logger.info("Starting ffmpeg: %s", " ".join(ffmpeg_command))

        await asyncio.create_subprocess_exec(*ffmpeg_command)

        stream_app = StaticFilesWithCORS(StaticFiles(directory=stream_dir))

        app.mount("/stream", stream_app)

        local_ip = get_local_ip()

        task = asyncio.create_task(subscribe(chromecast.media_controller, local_ip))

        yield

        task.cancel()
        await sink_controller.close()


def get_media_controller(request: Request) -> MediaController:
    return typing.cast("MediaController", request.app.state.media_controller)  # pyright: ignore[reportAny]


async def pause(request: Request) -> Response:
    media_controller = get_media_controller(request)
    media_controller.pause()
    return Response(content="OK")


async def play(request: Request) -> Response:
    media_controller = get_media_controller(request)
    media_controller.play()
    media_controller.seek(None)  # type: ignore[arg-type] # pyright: ignore[reportArgumentType]
    return Response(content="OK")


def create_app() -> Starlette:
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger(__name__).setLevel(logging.INFO)
    # logger = logging.getLogger(__name__)
    chromecast = find_chromecast()

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
        ],
        lifespan=partial(lifespan, chromecast=chromecast),
        middleware=middleware,
    )
