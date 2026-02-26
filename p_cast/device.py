import asyncio
import contextlib
import logging
import re
import typing
from collections.abc import Callable, Coroutine
from typing import override
from uuid import UUID

import pulsectl
import pulsectl_asyncio
from pychromecast import Chromecast
from pychromecast.controllers.receiver import CastStatus, CastStatusListener

from p_cast.exceptions import SinkError

logger = logging.getLogger(__name__)

type ActivationCallback = Callable[[str], Coroutine[None, None, None]]
type DeactivationCallback = Callable[[str], Coroutine[None, None, None]]


_SINK_NAME_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

_MIN_AUDIBLE_VOLUME = 0.1


def _make_sink_name(chromecast_name: str) -> str:
    sanitized = _SINK_NAME_INVALID_CHARS.sub("_", chromecast_name).lower()
    return f"{sanitized}_cast"


def _make_friendly_sink_name(chromecast_name: str) -> str:
    return f"{chromecast_name} Cast"


class _CastVolumeSync(CastStatusListener):
    """Bridges Chromecast-initiated volume/mute changes back to the PA sink.

    new_cast_status() runs on pychromecast's socket thread; the apply
    coroutine is scheduled onto the event loop via run_coroutine_threadsafe.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_volume_change: Callable[[float, bool], Coroutine[None, None, None]],
    ) -> None:
        self._loop = loop
        self._on_volume_change = on_volume_change
        self._active = True

    def deactivate(self) -> None:
        self._active = False

    @override
    def new_cast_status(self, status: CastStatus) -> None:
        if not self._active:
            return
        level = status.volume_level
        muted = status.volume_muted
        if level is None:
            return
        asyncio.run_coroutine_threadsafe(self._on_volume_change(level, muted), self._loop)


class SinkController:
    """Manages a PulseAudio null sink for a single Chromecast device.

    Creates the sink eagerly at startup. Volume/mute sync to the Chromecast
    is started lazily when the sink becomes active.
    """

    _sink_module_id: int
    _volume_listener: asyncio.Task[None]

    def __init__(self, chromecast: Chromecast) -> None:
        self.cast = chromecast
        self.sink_name = _make_sink_name(chromecast.name)  # type: ignore[arg-type]
        self._sink_friendly_name = _make_friendly_sink_name(chromecast.name)  # type: ignore[arg-type]
        self._sink_module_id = -1
        self.available = True
        logger.info(
            "New cc device %s at %s:%d registered: %s -> sink %s",
            chromecast.uuid,
            chromecast.cast_info.host,
            chromecast.cast_info.port,
            chromecast.name,
            self.sink_name,
        )

    async def init(self) -> None:
        self._pulse = pulsectl_asyncio.PulseAsync(f"p-cast-{self.sink_name}")
        await self._pulse.connect()
        self._sink_module_id = await self.create_sink(self.sink_name, self._sink_friendly_name)

    async def start_volume_sync(self) -> None:
        self._synced_volume: float | None = None
        self._synced_mute: bool | None = None
        self._volume_listener = asyncio.create_task(self._subscribe_volume())
        self._volume_listener.add_done_callback(self._on_volume_listener_done)
        loop = asyncio.get_running_loop()
        self._cast_volume_sync = _CastVolumeSync(loop=loop, on_volume_change=self._apply_cast_volume)
        self.cast.register_status_listener(self._cast_volume_sync)

    @staticmethod
    def _on_volume_listener_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Volume sync crashed: %s", exc, exc_info=exc)

    async def stop_volume_sync(self) -> None:
        if hasattr(self, "_cast_volume_sync"):
            self._cast_volume_sync.deactivate()
        if hasattr(self, "_volume_listener"):
            self._volume_listener.cancel()

    async def create_sink(self, name: str, friendly_name: str) -> int:
        sink_properties = [
            f"device.description='{friendly_name}'",
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
            await self._pulse.get_sink_by_name(self.sink_name),
        )

    @property
    def device_id(self) -> UUID:
        return self.cast.uuid

    async def get_sink_name(self) -> str:
        sink = await self.get_sink()
        return sink.name  # type: ignore[no-any-return]

    async def remove_sink(self, *, stale_ok: bool = False) -> None:
        """Unload the PA sink module and close the pulse connection.

        The controller stays in the controllers dict so it can be restored
        via init() when the device becomes reachable again.

        In case we're shutting down, a stale pulse connection is untroubling; set to True.
        """
        if hasattr(self, "_volume_listener"):
            self._volume_listener.cancel()
        if self._sink_module_id != -1:
            try:
                await self._pulse.module_unload(self._sink_module_id)
            except Exception:
                logger.log(
                    logging.INFO if stale_ok else logging.WARNING,
                    "Failed to unload PA module %d for sink %s",
                    self._sink_module_id,
                    self.sink_name,
                    exc_info=(not stale_ok),
                )
            self._sink_module_id = -1
        self._pulse.close()

    async def close(self) -> None:
        """Shutdown cleanup — best-effort, pulse connection may be stale."""
        await self.remove_sink(stale_ok=True)

    def get_volume(self, sink: pulsectl.PulseSinkInfo) -> float:
        return typing.cast("int", sink.volume.values[0])

    def get_mute(self, sink: pulsectl.PulseSinkInfo) -> bool:
        return bool(sink.mute)  # pyright: ignore[reportAttributeAccessIssue]

    async def _apply_cast_volume(self, level: float, muted: bool) -> None:  # noqa: FBT001
        """Apply a Chromecast-initiated volume/mute change to the PA sink.

        Skips the update if the values are already in sync (e.g. an echo of our
        own set_volume call), preventing feedback loops with _subscribe_volume.
        """
        volume_epsilon = 0.005  # less than a single volume-up/down step
        level_changed = self._synced_volume is None or abs(level - self._synced_volume) > volume_epsilon
        mute_changed = self._synced_mute is None or muted != self._synced_mute
        if not level_changed and not mute_changed:
            return
        try:
            sink = await self.get_sink()
            if level_changed:
                logger.debug("Cast volume change → PA sink %s: %.3f", self.sink_name, level)
                self._synced_volume = level
                await self._pulse.volume_set_all_chans(sink, level)
            if mute_changed:
                logger.debug("Cast mute change → PA sink %s: %s", self.sink_name, muted)
                self._synced_mute = muted
                await self._pulse.mute(sink, mute=muted)
        except Exception:
            logger.warning("Failed to apply cast volume to PA sink", exc_info=True)

    async def _subscribe_volume(self) -> None:  # noqa: C901
        sink = await self.get_sink()

        # Initialize sink volume from Chromecast's current state, ensuring it is at least audible
        try:
            cast_volume = self.cast.status.volume_level  # type: ignore[union-attr]
            cast_mute = self.cast.status.volume_muted  # type: ignore[union-attr]
            if cast_mute:
                self.cast.set_volume_muted(muted=False)
            if cast_volume is not None and cast_volume < _MIN_AUDIBLE_VOLUME:
                self.cast.set_volume(_MIN_AUDIBLE_VOLUME)
                cast_volume = _MIN_AUDIBLE_VOLUME
            if cast_volume is not None:
                await self._pulse.volume_set_all_chans(sink, cast_volume)
            await self._pulse.mute(sink, mute=False)
            sink = await self.get_sink()
        except Exception:
            logger.warning("Failed to initialize sink volume from Chromecast", exc_info=True)

        self._synced_volume = self.get_volume(sink)
        self._synced_mute = self.get_mute(sink)

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
                if changed_volume != self._synced_volume:
                    self.cast.set_volume(volume=changed_volume)
                    self._synced_volume = changed_volume
                if changed_mute != self._synced_mute:
                    self.cast.set_volume_muted(muted=changed_mute)
                    self._synced_mute = changed_mute
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
        self._sink_indices: dict[int, str] = {}
        self._our_sink_indices: set[int] = set()
        self._deactivation_task: asyncio.Task[None] | None = None

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

    def _cancel_deferred_deactivation(self) -> None:
        if self._deactivation_task is not None:
            self._deactivation_task.cancel()
            self._deactivation_task = None

    async def stop(self) -> None:
        self._cancel_deferred_deactivation()
        if hasattr(self, "_task"):
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if hasattr(self, "_pulse"):
            self._pulse.close()
        if hasattr(self, "_query_pulse"):
            self._query_pulse.close()

    async def refresh_sink_indices(self) -> None:
        """Re-query all controllers to rebuild the sink index mapping."""
        mapping: dict[int, str] = {}
        for sink_name, controller in self._controllers.items():
            if not controller.available:
                continue
            try:
                sink = await controller.get_sink()
                mapping[sink.index] = sink_name  # pyright: ignore[reportAttributeAccessIssue]
            except SinkError:
                pass  # available=True but init() not yet complete; next refresh will pick it up
            except Exception:
                logger.warning("Failed to get sink for %s", sink_name, exc_info=True)
        self._sink_indices = mapping
        self._our_sink_indices = set(mapping.keys())

    async def _check_existing_sink_inputs(self) -> None:
        """Scan existing sink-inputs for any already routed to our sinks."""
        sink_inputs = typing.cast(
            "list[pulsectl.PulseSinkInputInfo]",
            await self._query_pulse.sink_input_list(),
        )
        for si in sink_inputs:
            sink_idx: int = si.sink  # pyright: ignore[reportAttributeAccessIssue, reportAssignmentType]
            if sink_idx in self._our_sink_indices:
                sink_name = self._sink_indices[sink_idx]
                self._active_sink_inputs.add(si.index)  # pyright: ignore[reportAttributeAccessIssue]
                if self._active_sink != sink_name:
                    if self._active_sink is not None:
                        await self._on_deactivate(self._active_sink)
                    self._active_sink = sink_name
                    logger.info("Existing sink-input detected on: %s", sink_name)
                    await self._on_activate(sink_name)

    async def _monitor(self) -> None:
        await self.refresh_sink_indices()
        logger.info(
            "Sink-input monitor started, watching sink indices: %s",
            dict(self._sink_indices.items()),
        )

        await self._check_existing_sink_inputs()

        async for event in self._pulse.subscribe_events(
            pulsectl.PulseEventMaskEnum.sink_input,  # pyright: ignore[reportAttributeAccessIssue]
        ):
            logger.debug("sink_input event: %s", event)

            if event.t in {pulsectl.PulseEventTypeEnum.new, pulsectl.PulseEventTypeEnum.change}:  # pyright: ignore[reportAttributeAccessIssue]
                await self._handle_sink_input_update(
                    event.index,  # pyright: ignore[reportAttributeAccessIssue]
                )
            elif event.t == pulsectl.PulseEventTypeEnum.remove:  # pyright: ignore[reportAttributeAccessIssue]
                await self._handle_removed_sink_input(
                    event.index,  # pyright: ignore[reportAttributeAccessIssue]
                )

    async def _handle_sink_input_update(
        self,
        sink_input_index: int,
    ) -> None:
        sink_inputs = typing.cast(
            "list[pulsectl.PulseSinkInputInfo]",
            await self._query_pulse.sink_input_list(),
        )
        for si in sink_inputs:
            if si.index != sink_input_index:  # pyright: ignore[reportAttributeAccessIssue]
                continue
            sink_idx: int = si.sink  # pyright: ignore[reportAttributeAccessIssue, reportAssignmentType]
            if sink_idx in self._our_sink_indices:
                sink_name = self._sink_indices[sink_idx]
                self._cancel_deferred_deactivation()
                if self._active_sink != sink_name:
                    if self._active_sink is not None:
                        logger.info(
                            "Switching from %s to %s (only one sink can stream at a time)",
                            self._active_sink,
                            sink_name,
                        )
                        await self._on_deactivate(self._active_sink)
                    self._active_sink = sink_name
                    self._active_sink_inputs.clear()
                    logger.info("Sink activated: %s", sink_name)
                    await self._on_activate(sink_name)
                self._active_sink_inputs.add(sink_input_index)
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
            logger.info("Last sink-input removed, deferring deactivation: %s", self._active_sink)
            self._cancel_deferred_deactivation()
            self._deactivation_task = asyncio.create_task(
                self._deferred_deactivate(self._active_sink),
            )

    async def _deferred_deactivate(self, sink_name: str, delay_s: int = 15) -> None:
        """Wait briefly before deactivating to handle song transitions / avoid excessive reconnects."""
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        if not self._active_sink_inputs and self._active_sink == sink_name:
            logger.info("Sink deactivated (after grace period): %s", sink_name)
            await self._on_deactivate(sink_name)
            self._active_sink = None
        self._deactivation_task = None

    def clear_active(self) -> None:
        """Reset active state so the monitor can re-detect existing sink-inputs."""
        self._cancel_deferred_deactivation()
        self._active_sink = None
        self._active_sink_inputs.clear()

    @property
    def active_sink(self) -> str | None:
        return self._active_sink
