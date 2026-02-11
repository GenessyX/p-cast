import asyncio
import contextlib
import logging
import typing
from collections.abc import Callable, Coroutine

import pulsectl
import pulsectl_asyncio
from pychromecast import Chromecast

from p_cast.exceptions import SinkError

logger = logging.getLogger(__name__)

type ActivationCallback = Callable[[str], Coroutine[None, None, None]]
type DeactivationCallback = Callable[[str], Coroutine[None, None, None]]


class SinkController:
    """Manages a PulseAudio null sink for a single Chromecast device.

    Creates the sink eagerly at startup. Volume/mute sync to the Chromecast
    is started lazily when the sink becomes active.
    """

    _sink_module_id: int
    _volume_listener: asyncio.Task[None]

    def __init__(self, chromecast: Chromecast, sink_name: str) -> None:
        self._cast = chromecast
        self._sink_name = sink_name
        self._sink_module_id = -1

    async def init(self) -> None:
        self._pulse = pulsectl_asyncio.PulseAsync(f"p-cast-{self._sink_name}")
        await self._pulse.connect()
        self._sink_module_id = await self.create_sink(self._sink_name)

    async def start_volume_sync(self) -> None:
        self._volume_listener = asyncio.create_task(self._subscribe_volume())
        self._volume_listener.add_done_callback(self._on_volume_listener_done)

    @staticmethod
    def _on_volume_listener_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Volume sync crashed: %s", exc, exc_info=exc)

    async def stop_volume_sync(self) -> None:
        if hasattr(self, "_volume_listener"):
            self._volume_listener.cancel()

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
            await self._pulse.get_sink_by_name(self._sink_name),
        )

    async def get_sink_name(self) -> str:
        sink = await self.get_sink()
        return sink.name  # pyright: ignore[reportAttributeAccessIssue]

    async def close(self) -> None:
        if hasattr(self, "_volume_listener"):
            self._volume_listener.cancel()
        try:
            await self._pulse.module_unload(self._sink_module_id)
        except Exception:
            logger.warning(
                "Failed to unload PA module %d for sink %s",
                self._sink_module_id,
                self._sink_name,
                exc_info=True,
            )
        self._pulse.close()

    def get_volume(self, sink: pulsectl.PulseSinkInfo) -> float:
        return typing.cast("int", sink.volume.values[0])

    def get_mute(self, sink: pulsectl.PulseSinkInfo) -> bool:
        return bool(sink.mute)  # pyright: ignore[reportAttributeAccessIssue]

    async def _subscribe_volume(self) -> None:
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
            try:
                if changed_volume != current_volume:
                    self._cast.set_volume(volume=changed_volume)
                    current_volume = changed_volume
                if changed_mute != current_mute:
                    self._cast.set_volume_muted(muted=changed_mute)
                    current_mute = changed_mute
            except Exception:
                logger.warning("Failed to sync volume to Chromecast", exc_info=True)


class SinkInputMonitor:
    """Watches PulseAudio sink-input events to detect when audio is routed to one of our sinks.

    Fires activation/deactivation callbacks so the app can lazily start or stop
    the FFmpeg + Chromecast streaming pipeline. Handles new, moved (change),
    and removed sink-inputs.
    """

    def __init__(
        self,
        controllers: dict[str, SinkController],
        on_activate: ActivationCallback,
        on_deactivate: DeactivationCallback,
    ) -> None:
        self._controllers = controllers
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate
        self._active_sink: str | None = None
        self._active_sink_inputs: set[int] = set()

    async def start(self) -> None:
        self._pulse = pulsectl_asyncio.PulseAsync("p-cast-monitor")
        await self._pulse.connect()
        self._query_pulse = pulsectl_asyncio.PulseAsync("p-cast-monitor-query")
        await self._query_pulse.connect()
        self._task = asyncio.create_task(self._monitor())
        self._task.add_done_callback(self._on_monitor_done)

    @staticmethod
    def _on_monitor_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Sink-input monitor crashed: %s", exc, exc_info=exc)

    async def stop(self) -> None:
        if hasattr(self, "_task"):
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if hasattr(self, "_pulse"):
            self._pulse.close()
        if hasattr(self, "_query_pulse"):
            self._query_pulse.close()

    async def _get_sink_indices(self) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for sink_name, controller in self._controllers.items():
            sink = await controller.get_sink()
            mapping[sink.index] = sink_name  # pyright: ignore[reportAttributeAccessIssue]
        return mapping

    async def _monitor(self) -> None:
        sink_indices = await self._get_sink_indices()
        our_sink_indices = set(sink_indices.keys())
        logger.info(
            "Sink-input monitor started, watching sink indices: %s",
            {idx: name for idx, name in sink_indices.items()},
        )

        async for event in self._pulse.subscribe_events(
            pulsectl.PulseEventMaskEnum.sink_input,  # pyright: ignore[reportAttributeAccessIssue]
        ):
            logger.debug("sink_input event: %s", event)

            if event.t == pulsectl.PulseEventTypeEnum.new:  # pyright: ignore[reportAttributeAccessIssue]
                await self._handle_sink_input_update(
                    event.index,  # pyright: ignore[reportAttributeAccessIssue]
                    our_sink_indices,
                    sink_indices,
                )
            elif event.t == pulsectl.PulseEventTypeEnum.change:  # pyright: ignore[reportAttributeAccessIssue]
                await self._handle_sink_input_update(
                    event.index,  # pyright: ignore[reportAttributeAccessIssue]
                    our_sink_indices,
                    sink_indices,
                )
            elif event.t == pulsectl.PulseEventTypeEnum.remove:  # pyright: ignore[reportAttributeAccessIssue]
                await self._handle_removed_sink_input(
                    event.index,  # pyright: ignore[reportAttributeAccessIssue]
                )

    async def _handle_sink_input_update(
        self,
        sink_input_index: int,
        our_sink_indices: set[int],
        sink_indices: dict[int, str],
    ) -> None:
        sink_inputs = typing.cast(
            "list[pulsectl.PulseSinkInputInfo]",
            await self._query_pulse.sink_input_list(),
        )
        for si in sink_inputs:
            if si.index != sink_input_index:  # pyright: ignore[reportAttributeAccessIssue]
                continue
            sink_idx: int = si.sink  # pyright: ignore[reportAttributeAccessIssue, reportAssignmentType]
            if sink_idx in our_sink_indices:
                sink_name = sink_indices[sink_idx]
                self._active_sink_inputs.add(sink_input_index)
                if self._active_sink != sink_name:
                    if self._active_sink is not None:
                        await self._on_deactivate(self._active_sink)
                    self._active_sink = sink_name
                    logger.info("Sink activated: %s", sink_name)
                    await self._on_activate(sink_name)
            elif sink_input_index in self._active_sink_inputs:
                # Sink-input moved away from our sink to a non-monitored sink
                self._active_sink_inputs.discard(sink_input_index)
                if not self._active_sink_inputs and self._active_sink is not None:
                    logger.info("Sink deactivated (moved away): %s", self._active_sink)
                    await self._on_deactivate(self._active_sink)
                    self._active_sink = None
            break

    async def _handle_removed_sink_input(self, sink_input_index: int) -> None:
        if sink_input_index not in self._active_sink_inputs:
            return
        self._active_sink_inputs.discard(sink_input_index)
        if not self._active_sink_inputs and self._active_sink is not None:
            logger.info("Sink deactivated: %s", self._active_sink)
            await self._on_deactivate(self._active_sink)
            self._active_sink = None

    def clear_active(self) -> None:
        """Reset active state so the monitor can re-detect existing sink-inputs."""
        self._active_sink = None
        self._active_sink_inputs.clear()

    @property
    def active_sink(self) -> str | None:
        return self._active_sink
