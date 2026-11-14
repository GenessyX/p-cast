from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from contextlib import AbstractAsyncContextManager
    from types import SimpleNamespace
    from unittest.mock import Mock

    from starlette.applications import Starlette


class Encoder:
    def __init__(self, command: tuple[str, ...]) -> None:
        self.command = command
        self.returncode: int | None = None
        self.stderr = asyncio.StreamReader()
        self.exited = asyncio.Event()

    def finish(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.exited.set()

    def terminate(self) -> None:
        self.finish(-15)

    async def wait(self) -> int:
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode


@dataclass
class Runtime:
    sinks: dict[str, SimpleNamespace] = field(default_factory=dict)
    inputs: list[SimpleNamespace] = field(default_factory=list)
    pulses: dict[str, Mock] = field(default_factory=dict)
    encoders: list[Encoder] = field(default_factory=list)
    casts: list[Mock] = field(default_factory=list)
    waits: dict[UUID, Callable[[], Awaitable[None] | None]] = field(default_factory=dict)
    app_ids: dict[UUID, str | None] = field(default_factory=dict)
    addresses: dict[UUID, tuple[str, int]] = field(
        default_factory=lambda: {
            UUID(int=1): ("192.0.2.10", 8009),
            UUID(int=2): ("192.0.2.11", 8009),
        },
    )
    app: Starlette = field(init=False)
    discovery: Mock = field(init=False)
    until: Callable[[Callable[[], bool]], Awaitable[None]] = field(init=False)
    select: Callable[[str], Coroutine[None, None, None]] = field(init=False)
    update: Callable[[UUID], Coroutine[None, None, None]] = field(init=False)
    start: Callable[[], AbstractAsyncContextManager[Runtime]] = field(init=False)
