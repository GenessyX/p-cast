import asyncio
import contextlib
import logging
import tempfile
import typing
from collections.abc import AsyncIterator
from functools import partial
from typing import override

from pychromecast.controllers.media import MediaController
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from cast import find_media_controller, get_local_ip, subscribe_to_stream
from ffmpeg import create_ffmpeg_stream_command

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
        del response.headers["accept-ranges"]
        return response


@contextlib.asynccontextmanager
async def lifespan(
    app: Starlette,
    media_controller: MediaController,
) -> AsyncIterator[None]:
    async def subscribe(mc: MediaController, local_ip: str) -> None:
        await asyncio.sleep(2)
        subscribe_to_stream(mc, local_ip)

    with tempfile.TemporaryDirectory() as stream_dir:
        ffmpeg_command = create_ffmpeg_stream_command(
            sink="sink_name.monitor",
            stream_dir=stream_dir,
            bitrate="512k",
        )

        await asyncio.create_subprocess_exec(*ffmpeg_command)

        stream_app = StaticFilesWithCORS(StaticFiles(directory=stream_dir))

        app.mount("/stream", stream_app)

        local_ip = get_local_ip()

        task = asyncio.create_task(subscribe(media_controller, local_ip))

        yield

        media_controller.stop(timeout=1)
        task.cancel()


def create_app() -> Starlette:
    logging.basicConfig(level=logging.DEBUG)
    mc = find_media_controller()

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_headers=["*"],
            allow_credentials=True,
            allow_methods=["*"],
        ),
    ]

    return Starlette(
        lifespan=partial(lifespan, media_controller=mc),
        middleware=middleware,
    )
