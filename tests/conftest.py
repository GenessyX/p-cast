import asyncio
import inspect
import os
import shlex
import signal
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pulsectl
import pulsectl_asyncio
import pytest
from pychromecast.controllers.media import MediaController

from p_cast import app, tasks
from p_cast.cast import CastDiscovery
from tests.helpers import Encoder, Runtime


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110 - yield until observable mock state changes, bounded by timeout
            await asyncio.sleep(0)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Runtime:  # noqa: C901, PLR0915
    pulse_type = pulsectl_asyncio.PulseAsync
    state = Runtime()

    def pulse_factory(name: str) -> Mock:  # noqa: C901
        pulse = Mock(spec=pulse_type)
        events: asyncio.Queue[SimpleNamespace] = asyncio.Queue()
        pulse.ready = asyncio.Event()
        pulse.events = events

        def get_sink(sink_name: str) -> SimpleNamespace:
            if sink_name not in state.sinks:
                raise pulsectl.PulseIndexError(sink_name)
            return state.sinks[sink_name]

        def load_module(_module: str, arguments: str) -> int:
            sink_name = shlex.split(arguments)[0].split("=", 1)[1]
            index = 1 if sink_name == "a_cast" else 2
            state.sinks[sink_name] = SimpleNamespace(
                name=sink_name,
                index=index,
                owner_module=index,
                sample_spec=SimpleNamespace(rate=48000),
                volume=SimpleNamespace(values=[0.5]),
                mute=False,
            )
            return index

        def unload_module(index: int) -> None:
            for sink_name, sink in list(state.sinks.items()):
                if sink.owner_module == index:
                    del state.sinks[sink_name]

        def change_volume(sink: SimpleNamespace, volume: float) -> None:
            sink.volume.values[:] = [volume]
            events.put_nowait(SimpleNamespace(index=sink.index, t=pulsectl.PulseEventTypeEnum.change))

        def change_mute(sink: SimpleNamespace, *, mute: bool) -> None:
            sink.mute = mute
            events.put_nowait(SimpleNamespace(index=sink.index, t=pulsectl.PulseEventTypeEnum.change))

        async def subscribe(_mask: object) -> AsyncIterator[SimpleNamespace]:
            pulse.ready.set()
            while True:
                yield await events.get()

        pulse.connect = AsyncMock()
        pulse.get_sink_by_name = AsyncMock(side_effect=get_sink)
        pulse.module_load = AsyncMock(side_effect=load_module)
        pulse.module_unload = AsyncMock(side_effect=unload_module)
        pulse.module_info = AsyncMock(return_value=SimpleNamespace(name="module-null-sink"))
        pulse.sink_input_list = AsyncMock(side_effect=lambda: list(state.inputs))
        pulse.volume_set_all_chans = AsyncMock(side_effect=change_volume)
        pulse.mute = AsyncMock(side_effect=change_mute)
        pulse.subscribe_events = subscribe
        state.pulses[name] = pulse
        return pulse

    def make_cast(device_id: UUID) -> Mock:
        host, port = state.addresses[device_id]
        cast = Mock()
        cast.name = "A" if device_id.int == 1 else "B"
        cast.uuid = device_id
        cast.cast_info = SimpleNamespace(host=host, port=port)
        cast.app_id = state.app_ids.get(device_id)
        cast.status = SimpleNamespace(volume_level=0.5, volume_muted=False)
        cast.attach_mock(Mock(spec=MediaController), "media_controller")
        cast.wait.side_effect = lambda **_kwargs: state.waits.get(device_id, lambda: None)()
        state.casts.append(cast)
        return cast

    async def to_thread(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        result = function(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def start_encoder(*command: str, **_kwargs: object) -> Encoder:
        encoder = Encoder(command)
        state.encoders.append(encoder)
        return encoder

    discovery = Mock(spec=CastDiscovery)
    discovery.discover.side_effect = lambda: [make_cast(device_id) for device_id in state.addresses]
    discovery.create_chromecast.side_effect = make_cast
    discovery.get_device_address.side_effect = state.addresses.get
    state.discovery = discovery
    state.until = until
    monkeypatch.setattr(app, "CastDiscovery", lambda: discovery)
    monkeypatch.setattr(pulsectl_asyncio, "PulseAsync", pulse_factory)
    monkeypatch.setattr(app, "get_local_ip", lambda: "192.0.2.1")
    monkeypatch.setattr(app, "_sd_notify", Mock())
    monkeypatch.setattr(signal, "pthread_sigmask", Mock())
    monkeypatch.setattr(os, "kill", Mock(side_effect=AssertionError("Unexpected process signal")))
    monkeypatch.setattr(os, "_exit", Mock(side_effect=AssertionError("Unexpected process exit")))
    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_encoder)
    for delay in ("_FFMPEG_STARTUP_DELAY", "_STREAM_SUBSCRIBE_DELAY", "_APP_QUIT_DELAY", "_FFMPEG_RESTART_DELAY"):
        monkeypatch.setattr(app, delay, 0)
    for name, value in {
        "PCAST_LOG_LEVEL": "INFO",
        "PCAST_PORT": "3000",
        "PCAST_BITRATE": "256k",
        "PCAST_FFMPEG": "ffmpeg",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PCAST_LOG_FILE", raising=False)
    monkeypatch.delenv("PCAST_SINK_GAIN", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    async def select(sink_name: str) -> None:
        sink = state.sinks[sink_name]
        state.inputs[:] = [SimpleNamespace(index=42, sink=sink.index)]
        await state.app.state.monitor._handle_sink_input_update(42)

    async def update(device_id: UUID) -> None:
        discovery.set_callbacks.call_args.kwargs["on_update"](device_id)
        await asyncio.sleep(0)
        await until(lambda: not tasks._background_tasks)

    state.select = select
    state.update = update

    @asynccontextmanager
    async def start() -> AsyncIterator[Runtime]:
        existing_tasks = asyncio.all_tasks()
        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", Mock())
        state.app = app.create_app()
        try:
            context = state.app.router.lifespan_context(state.app)
            await context.__aenter__()
            try:
                await asyncio.wait_for(state.pulses["p-cast-monitor"].ready.wait(), timeout=5)
                yield state
            finally:
                await asyncio.wait_for(context.__aexit__(None, None, None), timeout=5)
        finally:
            pending = asyncio.all_tasks() - existing_tasks
            for task in pending:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)
            for encoder in state.encoders:
                if encoder.returncode is None:
                    encoder.terminate()

    state.start = start
    return state
