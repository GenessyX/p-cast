import asyncio
import platform
from pathlib import Path
from uuid import UUID

import pytest
from pychromecast.config import APP_BACKDROP, APP_MEDIA_RECEIVER
from pychromecast.controllers.media import STREAM_TYPE_LIVE
from starlette.routing import Mount

from tests.helpers import Runtime


async def test_switch_during_reconnect_keeps_only_selected_stream(runtime: Runtime) -> None:
    async with runtime.start():
        await runtime.select("a_cast")
        monitor = runtime.app.state.monitor
        old_cast = runtime.app.state.active_controller.cast
        recover = old_cast.media_controller.register_status_listener.call_args.args[0]._on_recover
        connecting, release, switching = asyncio.Event(), asyncio.Event(), asyncio.Event()
        waits = 0

        async def wait_for_cast() -> None:
            nonlocal waits
            waits += 1
            # The health probe succeeds; hold the subsequent reconnect open.
            if waits == 2:
                connecting.set()
                await release.wait()

        deactivate = monitor._on_deactivate

        async def observe_switch(sink_name: str) -> None:
            switching.set()
            await deactivate(sink_name)

        monitor._on_deactivate = observe_switch
        runtime.waits[UUID(int=1)] = wait_for_cast
        runtime.addresses[UUID(int=1)] = ("192.0.2.30", 8009)
        reconnect = asyncio.create_task(runtime.update(UUID(int=1)))
        await asyncio.wait_for(connecting.wait(), timeout=5)
        switch = asyncio.create_task(runtime.select("b_cast"))
        await asyncio.wait_for(switching.wait(), timeout=5)
        # Let the switch advance if serialization has been broken.
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(reconnect, switch), timeout=5)

        assert monitor.active_sink == "b_cast"
        assert runtime.app.state.active_controller.sink_name == "b_cast"
        assert sum(p.returncode is None for p in runtime.encoders) == 1
        assert [r.path for r in runtime.app.routes if isinstance(r, Mount)].count("/stream") == 1
        encoders = len(runtime.encoders)
        await recover("a_cast")
        await deactivate("a_cast")
        assert runtime.app.state.active_controller.sink_name == "b_cast"
        assert len(runtime.encoders) == encoders


@pytest.mark.parametrize("failure", ["unavailable", "pulse_error"])
async def test_failed_activation_can_retry(runtime: Runtime, failure: str, tmp_path: Path) -> None:
    async with runtime.start():
        controller = runtime.app.state.controllers["a_cast"]
        pulse = runtime.pulses["p-cast-a_cast"]
        lookup = pulse.get_sink_by_name.side_effect
        if failure == "unavailable":
            controller.available = False
        else:
            pulse.get_sink_by_name.side_effect = OSError("PulseAudio disconnected")
        await runtime.select("a_cast")
        assert runtime.app.state.monitor.active_sink is None
        assert runtime.app.state.active_controller is None
        assert not runtime.encoders
        assert not list(tmp_path.iterdir())  # noqa: ASYNC240 - local test directory

        controller.available = True
        pulse.get_sink_by_name.side_effect = lookup
        await runtime.select("a_cast")
        cast = controller.cast
        await runtime.until(lambda: cast.media_controller.play_media.called)
        assert runtime.app.state.monitor.active_sink == "a_cast"
        assert runtime.app.state.active_controller is controller


@pytest.mark.parametrize(
    ("host", "restarts"),
    [("192.0.2.30", True), ("::ffff:c000:20a", False), ("[::ffff:c000:20a]", False)],
)
async def test_address_update_preserves_sink(runtime: Runtime, host: str, *, restarts: bool) -> None:
    async with runtime.start():
        await runtime.select("a_cast")
        sink = runtime.sinks["a_cast"]
        runtime.addresses[UUID(int=1)] = (host, 8009)
        await runtime.update(UUID(int=1))
        assert runtime.sinks["a_cast"] is sink
        assert runtime.app.state.active_controller.sink_name == "a_cast"
        assert len(runtime.encoders) == (2 if restarts else 1)
        runtime.pulses["p-cast-a_cast"].module_unload.assert_not_called()


@pytest.mark.parametrize("app_id", [None, APP_BACKDROP, APP_MEDIA_RECEIVER, "705D30C6"])
async def test_subscription_quits_foreign_app_and_loads_live_stream(runtime: Runtime, app_id: str | None) -> None:
    runtime.app_ids[UUID(int=1)] = app_id
    async with runtime.start():
        await runtime.select("a_cast")
        cast = runtime.app.state.active_controller.cast
        await runtime.until(lambda: cast.media_controller.play_media.called)
        cast.media_controller.play_media.assert_called_once_with(
            "http://192.0.2.1:3000/stream/index.m3u8",
            content_type="application/vnd.apple.mpegurl",
            title=f"p-cast stream from {platform.node()}",
            stream_type=STREAM_TYPE_LIVE,
            media_info={"hlsSegmentFormat": "ts_aac"},
        )
        if app_id == "705D30C6":
            cast.quit_app.assert_called_once()
            calls = [call[0] for call in cast.mock_calls]
            assert calls.index("quit_app") < calls.index("media_controller.play_media")
        else:
            cast.quit_app.assert_not_called()


async def test_encoder_exit_recovers_without_cancelling_its_watcher(
    runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    async with runtime.start():
        await runtime.select("a_cast")
        cast = runtime.app.state.active_controller.cast
        await runtime.until(lambda: cast.media_controller.play_media.called)
        watcher = next(
            t for t in asyncio.all_tasks() if (coro := t.get_coro()) is not None and coro.__name__ == "watch_ffmpeg"
        )
        encoder = runtime.encoders[0]
        directory = Path(encoder.command[-1]).parent
        encoder.finish(1, b"encoder lost its audio input\n")
        await asyncio.wait_for(asyncio.shield(watcher), timeout=5)
        cast = runtime.app.state.active_controller.cast
        await runtime.until(lambda: cast.media_controller.play_media.called)
        assert not watcher.cancelled()
        assert not directory.exists()
        assert len(runtime.encoders) == 2
        assert sum(p.returncode is None for p in runtime.encoders) == 1
        assert "encoder lost its audio input" in caplog.text


async def test_normal_teardown_cleans_resources_without_restarting(runtime: Runtime) -> None:
    async with runtime.start():
        await runtime.select("a_cast")
        controller = runtime.app.state.active_controller
        await runtime.until(lambda: controller.cast.media_controller.play_media.called)
        watchers = [
            t for t in asyncio.all_tasks() if (coro := t.get_coro()) is not None and coro.__name__ == "watch_ffmpeg"
        ]
        directory = Path(runtime.encoders[0].command[-1]).parent
        await runtime.app.state.monitor._on_deactivate("a_cast")
        await asyncio.wait_for(asyncio.gather(*watchers, return_exceptions=True), timeout=5)
        assert runtime.app.state.active_controller is None
        assert all(p.returncode is not None for p in runtime.encoders)
        assert len(runtime.encoders) == 1
        assert "/stream" not in [r.path for r in runtime.app.routes if isinstance(r, Mount)]
        assert not directory.exists()
        controller.cast.media_controller.stop.assert_called_once_with(timeout=1.0)
