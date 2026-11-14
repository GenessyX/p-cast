import asyncio
from types import SimpleNamespace

import pytest

from p_cast import tasks
from tests.helpers import Runtime


async def test_volume_and_mute_sync_without_feedback(runtime: Runtime) -> None:
    async with runtime.start():
        await runtime.select("a_cast")
        controller = runtime.app.state.active_controller
        pulse = runtime.pulses["p-cast-a_cast"]
        cast = controller.cast
        sink = runtime.sinks["a_cast"]
        await runtime.until(lambda: pulse.ready.is_set() and pulse.events.empty())
        listener = cast.register_status_listener.call_args.args[0]
        sending, reply = asyncio.Event(), asyncio.Event()

        async def send_volume(*, volume: float) -> None:
            assert volume == pytest.approx(0.7)
            sending.set()
            await reply.wait()

        cast.set_volume.side_effect = send_volume
        await pulse.volume_set_all_chans(sink, 0.7)
        await pulse.mute(sink, mute=True)
        await asyncio.wait_for(sending.wait(), timeout=5)
        # An older response during our request must not undo the desktop change.
        listener.new_cast_status(SimpleNamespace(volume_level=0.5, volume_muted=False))
        await asyncio.sleep(0)
        await runtime.until(lambda: not tasks._background_tasks)
        assert sink.volume.values[0] == pytest.approx(0.7)
        assert sink.mute is True
        reply.set()
        await runtime.until(lambda: controller._cast_requests_in_flight == 0 and pulse.events.empty())
        cast.set_volume.assert_called_once_with(volume=0.7)
        cast.set_volume_muted.assert_called_once_with(muted=True)

        pulse.volume_set_all_chans.reset_mock()
        pulse.mute.reset_mock()
        listener.new_cast_status(SimpleNamespace(volume_level=0.7, volume_muted=True))
        await asyncio.sleep(0)
        await runtime.until(lambda: not tasks._background_tasks)
        pulse.volume_set_all_chans.assert_not_called()
        pulse.mute.assert_not_called()

        listener.new_cast_status(SimpleNamespace(volume_level=0.35, volume_muted=False))
        await asyncio.sleep(0)
        await runtime.until(lambda: not tasks._background_tasks and pulse.events.empty())
        assert sink.volume.values[0] == pytest.approx(0.35)
        assert sink.mute is False
        pulse.volume_set_all_chans.assert_awaited_once_with(sink, 0.35)
        pulse.mute.assert_awaited_once_with(sink, mute=False)
        # PulseAudio's echoes of the device change do not generate more Cast requests.
        cast.set_volume.assert_called_once()
        cast.set_volume_muted.assert_called_once()
