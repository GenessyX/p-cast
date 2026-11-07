"""Live status of p-cast for UIs (``GET /status`` and the ``/events`` WebSocket).

Everything here is called on the event loop. It only records what the rest of the
app already does; it never changes behavior.

Each device has a *phase* with the time it started (``since``) and, when a timer is
running, the time it expires (``deadline``), so a UI can show a countdown. Phases:

- ``ready``: available, not streaming
- ``unavailable``: not reachable, waiting for mDNS to bring it back
- ``checking``: health check (Chromecast ``wait()``) on an idle device
- ``connecting``: activation, connecting to the Chromecast
- ``encoder``: FFmpeg starting
- ``subscribing``: waiting before sending LOAD (lets FFmpeg write the first segments)
- ``quitting_app``: quitting another receiver app before LOAD
- ``loading``: LOAD sent, waiting for the Chromecast to play
- ``playing`` / ``paused`` / ``idle``: Chromecast player state while streaming
- ``buffering``: Chromecast buffering; the deadline is the watchdog restart
- ``restarting``: stream restart scheduled (FFmpeg exited, watchdog, address change)
- ``lingering``: nothing plays into the sink; the deadline is the deferred deactivation
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

__all__ = ["StatusTracker", "status"]

_EVENT_LOG_SIZE = 50
_BUFFERING_WINDOW = 60.0  # seconds counted by "buffering_last_minute"
_SUBSCRIBER_QUEUE_SIZE = 100
_STREAMING_PHASES = frozenset(
    {
        "connecting",
        "encoder",
        "subscribing",
        "quitting_app",
        "loading",
        "playing",
        "paused",
        "idle",
        "buffering",
        "restarting",
        "lingering",
    },
)
_PLAYER_PHASES = {"PLAYING": "playing", "PAUSED": "paused", "IDLE": "idle", "BUFFERING": "buffering"}


@dataclass
class _Device:
    friendly_name: str
    phase: str = "ready"
    since: float = field(default_factory=time.time)
    deadline: float | None = None
    reason: str | None = None
    player_state: str | None = None
    check_deadline: float | None = None
    buffering_at: deque[float] = field(default_factory=lambda: deque(maxlen=100))

    def to_dict(self, sink_name: str, now: float) -> dict[str, Any]:
        while self.buffering_at and now - self.buffering_at[0] > _BUFFERING_WINDOW:
            self.buffering_at.popleft()
        return {
            "sink_name": sink_name,
            "friendly_name": self.friendly_name,
            "phase": self.phase,
            "since": self.since,
            "deadline": self.deadline,
            "reason": self.reason,
            "player_state": self.player_state,
            "checking_until": self.check_deadline,
            "buffering_last_minute": len(self.buffering_at),
        }


class StatusTracker:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._devices: dict[str, _Device] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=_EVENT_LOG_SIZE)
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._push_scheduled = False
        self.local_ip: str | None = None
        self.port: int | None = None
        self.active_sink: str | None = None
        self.source_app: str | None = None
        self.stream_started_at: float | None = None
        self.last_playlist_at: float | None = None
        self.last_segment_at: float | None = None

    # -- devices ---------------------------------------------------------------

    def add_device(self, sink_name: str, friendly_name: str | None, *, available: bool = True) -> None:
        friendly_name = friendly_name or sink_name
        device = self._devices.get(sink_name)
        if device is None:
            self._devices[sink_name] = _Device(friendly_name, phase="ready" if available else "unavailable")
        else:
            device.friendly_name = friendly_name
        self._changed()

    def set_phase(
        self,
        sink_name: str,
        phase: str,
        *,
        timeout: float | None = None,
        reason: str | None = None,
    ) -> None:
        device = self._devices.get(sink_name)
        if device is None:
            device = self._devices[sink_name] = _Device(sink_name)
        if device.phase == phase and timeout is None and reason is None:
            return
        now = time.time()
        if device.phase != phase:
            device.since = now
        device.phase = phase
        device.deadline = now + timeout if timeout is not None else None
        device.reason = reason
        if phase == "buffering":
            device.buffering_at.append(now)
        if phase in {"ready", "unavailable"}:
            device.player_state = None
            if self.active_sink == sink_name:
                self._set_active(None)
        elif phase == "connecting":
            self._set_active(sink_name)
        self._changed()

    def phase_of(self, sink_name: str) -> str | None:
        device = self._devices.get(sink_name)
        return device.phase if device else None

    def player_state(self, sink_name: str, state: str, buffering_timeout: float) -> None:
        """Chromecast media status of the streaming device (BUFFERING, PLAYING, ...).

        ``buffering_timeout`` is the watchdog delay, shown as the countdown while buffering.
        """
        device = self._devices.get(sink_name)
        if device is None or device.player_state == state:
            return
        device.player_state = state
        phase = _PLAYER_PHASES.get(state)
        if (
            phase is None
            # A restart or a deferred deactivation is pending: that is what the user waits for.
            or device.phase in {"restarting", "lingering", "ready", "unavailable"}
            # While loading, IDLE only means "nothing loaded yet".
            or (phase == "idle" and device.phase in {"connecting", "encoder", "subscribing", "quitting_app", "loading"})
        ):
            self._changed()
            return
        self.set_phase(sink_name, phase, timeout=buffering_timeout if phase == "buffering" else None)
        if phase == "buffering":
            self.event("warning", "buffering", "Chromecast is buffering", sink_name)

    def restore_player_phase(self, sink_name: str) -> None:
        """Leave ``lingering`` (audio came back) for the phase the player state implies."""
        device = self._devices.get(sink_name)
        if device is None or device.phase != "lingering":
            return
        phase = _PLAYER_PHASES.get(device.player_state or "", "loading")
        self.set_phase(sink_name, "playing" if phase == "buffering" else phase)

    def begin_check(self, sink_name: str, timeout: float) -> None:
        """Health check started. Idle devices show it as their phase."""
        device = self._devices.get(sink_name)
        if device is None:
            return
        device.check_deadline = time.time() + timeout
        if device.phase in {"ready", "unavailable"}:
            # Keep the previous phase as the reason so a UI can tell "restoring" from "re-checking".
            self.set_phase(sink_name, "checking", timeout=timeout, reason=device.phase)
        else:
            self._changed()

    def end_check(self, sink_name: str, *, available: bool) -> None:
        device = self._devices.get(sink_name)
        if device is None:
            return
        device.check_deadline = None
        if device.phase == "checking":
            self.set_phase(sink_name, "ready" if available else "unavailable")
        else:
            self._changed()

    # -- stream ----------------------------------------------------------------

    def set_source(self, app_name: str | None) -> None:
        if app_name != self.source_app:
            self.source_app = app_name
            self._changed()

    def _set_active(self, sink_name: str | None) -> None:
        if sink_name == self.active_sink:
            return
        self.active_sink = sink_name
        self.stream_started_at = time.time() if sink_name else None
        self.last_playlist_at = None
        self.last_segment_at = None
        if sink_name is None:
            self.source_app = None

    def http_fetch(self, path: str) -> None:
        """A file under /stream was served to the Chromecast."""
        now = time.time()
        if path.endswith(".m3u8"):
            self.last_playlist_at = now
        else:
            self.last_segment_at = now
        # Not pushed: a UI computes the age from the snapshot it gets on the next change
        # or polls /status. Pushing every segment would wake it every second.

    # -- events ----------------------------------------------------------------

    def event(self, level: str, code: str, text: str, sink_name: str | None = None) -> None:
        """Record something a user may want to know (``level``: info, warning, error)."""
        entry = {"ts": time.time(), "level": level, "code": code, "sink_name": sink_name, "text": text}
        self._events.append(entry)
        self._broadcast(json.dumps({"type": "event", "event": entry}))

    # -- output ----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "now": now,
            "started_at": self.started_at,
            "local_ip": self.local_ip,
            "port": self.port,
            "stream": {
                "sink_name": self.active_sink,
                "source_app": self.source_app,
                "started_at": self.stream_started_at,
                "last_playlist_at": self.last_playlist_at,
                "last_segment_at": self.last_segment_at,
            },
            "devices": [d.to_dict(name, now) for name, d in sorted(self._devices.items())],
            "events": list(self._events),
        }

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def _changed(self) -> None:
        # Coalesce bursts (e.g. several phase changes in one activation step) into one push.
        if self._push_scheduled or not self._subscribers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._push_scheduled = True
        loop.call_later(0.05, self._push_status)

    def status_only(self) -> dict[str, Any]:
        """Snapshot without the event log, which subscribers get one by one."""
        snapshot = self.snapshot()
        del snapshot["events"]
        return snapshot

    def _push_status(self) -> None:
        self._push_scheduled = False
        self._broadcast(json.dumps({"type": "status", "status": self.status_only()}))

    def _broadcast(self, message: str) -> None:
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):  # slow client: it gets a fresh snapshot later
                queue.put_nowait(message)


status = StatusTracker()
