import asyncio
import typing

import pulsectl
import pulsectl_asyncio
from pychromecast import Chromecast

from p_cast.exceptions import SinkError


class SinkController:
    _sink_module_id: int
    _listener: asyncio.Task[None]

    def __init__(self, chromecast: Chromecast) -> None:
        self._cast = chromecast
        self._sink_module_id = -1

    async def init(self) -> None:
        self._pulse = pulsectl_asyncio.PulseAsync("p-cast-client")
        await self._pulse.connect()
        self._sink_module_id = await self.create_sink("P-Cast")
        self._listener = asyncio.create_task(self.subscribe())

    async def create_sink(self, name: str) -> int:
        sink_properties = [
            f"device.description={name}",
            "channelmix.min-volume=5.0",
            "channelmix.max-volume=5.0",
            "channelmix.normalize=true",
        ]
        module_args = [
            f"sink_name={name}",
            f'sink_properties="{" ".join(sink_properties)}"',
        ]

        module_id = await self._pulse.module_load(
            "module-null-sink",
            " ".join(module_args),
        )
        return typing.cast("int", module_id)

    async def get_sink(self) -> pulsectl.PulseSinkInfo:
        if self._sink_module_id == -1:
            msg = "Uninitialized device"
            raise SinkError(msg)
        return typing.cast(
            "pulsectl.PulseSinkInfo",
            await self._pulse.get_sink_by_name("P-Cast"),
        )

    async def get_sink_name(self) -> str:
        sink = await self.get_sink()
        return sink.name  # pyright: ignore[reportAttributeAccessIssue]

    async def close(self) -> None:
        await self._pulse.module_unload(self._sink_module_id)
        self._listener.cancel()

    def get_volume(self, sink: pulsectl.PulseSinkInfo) -> float:
        return typing.cast("int", sink.volume.values[0])

    def get_mute(self, sink: pulsectl.PulseSinkInfo) -> bool:
        return bool(sink.mute)  # pyright: ignore[reportAttributeAccessIssue]

    async def subscribe(self) -> None:
        sink = await self.get_sink()
        current_volume = self.get_volume(sink)
        current_mute = self.get_mute(sink)

        async for event in self._pulse.subscribe_events(
            pulsectl.PulseEventMaskEnum.sink,  # pyright: ignore[reportAttributeAccessIssue]
        ):
            if event.index != sink.index:  # pyright: ignore[reportAttributeAccessIssue]
                continue
            if event.t != pulsectl.PulseEventTypeEnum.change:  # pyright: ignore[reportAttributeAccessIssue]
                continue

            changed_sink = await self.get_sink()
            changed_volume = self.get_volume(changed_sink)
            changed_mute = self.get_mute(changed_sink)
            if changed_volume != current_volume:
                self._cast.set_volume(volume=changed_volume)
                current_volume = changed_volume
            if changed_mute != current_mute:
                self._cast.set_volume_muted(muted=changed_mute)
                current_mute = changed_mute
