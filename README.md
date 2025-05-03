# P-Cast

**Cast anything you can hear on your Linux desktop to any Chromecast‑compatible speaker, TV or Nest device — live, with \~3 s latency.**

P‑Cast captures audio directly from PipeWire / PulseAudio, encodes it with FFmpeg and exposes the result as an HLS live stream that is played by Chromecast.

Casts local audio-device to chromecast compatible device.

## Quick start

```bash
git clone https://github.com/GenessyX/p-cast
cd p-cast

uv run python run.py
```

Send audio to **P‑Cast** (set it as default sink or route an app explicitly).

## Features

* **Virtual sink** - creates virtual null sink P-Cast on the fly.
* **Automatic device discovery** – finds the first Chromecast on your LAN via mDNS.
* **On‑the‑fly transcoding** – AAC @ 256 kbps by default (customisable via env vars).
* **Live HLS** – 0.5 s segments; Chromecast buffers 3 -> \~3 s end‑to‑end delay.
* **Volume follow** – changes to the PipeWire sink volume are mirrored to the Chromecast.
* **Tiny footprint** – single Python process + FFmpeg child; no browser or GUI required.
* **Optional reconnection guard** – a `p-cast-stream.conf` snippet keeps the capture stream pinned to the chosen device.

## How it works (TL;DR)

1. `run.py` starts [Granian](https://github.com/emmett-framework/granian) and serves the Starlette app on **:3000**.
2. `app.py`

   1. discovers a Chromecast (`cast.find_chromecast`),
   2. creates a **null sink** named `P‑Cast` (`device.SinkController`),
   3. launches FFmpeg (`ffmpeg.create_ffmpeg_stream_command`) to read from `P‑Cast.monitor` and write HLS segments to a temp dir,
   4. mounts that dir at **/stream**.
3. After 2 s the Chromecast receives `http://<host>:3000/stream/index.m3u8` via the Media Controller and starts buffering.
4. A background task listens for `pactl` volume‑change events and calls `Chromecast.set_volume(...)`.

Everything runs in a single Python process; FFmpeg is the only external binary.

## Requirements

| Component         | Purpose                     |
| ----------------- | --------------------------- |
| Linux w/ PipeWire | audio capture               |
| Python ≥ 3.12     | application runtime         |
| FFmpeg ≥ 6.1      | encoding & HLS muxing       |
| Chromecast        | playback device on same LAN |

Dependencies are declared in **pyproject.toml**. The examples below use [**uv**](https://github.com/astral-sh/uv) but regular `pip` works just as well.

```sh
uv run python run.py
```

## Optional: keep PipeWire from re‑connecting to another device

Unplugging headphones or switching default sink can make PipeWire migrate *all* streams – including the monitor the server is recording – to a new sink, resulting in streaming input from switched device to the Chromecast.
If that annoys you, add the supplied pipewire config:

```bash
mkdir -p ~/.config/pipewire/pipewire-pulse.conf.d
cp ./p-cast-stream.conf ~/.config/pipewire/pipewire-pulse.conf.d/p-cast-stream.conf
systemctl --user restart pipewire
```

The file sets:

```ini
node.dont-reconnect = true        # stay on the chosen device
node.latency        = "64/48000"  # optional, lowers internal latency
```

## 🔊 Audio Delay: PipeWire to Chromecast

The **minimum practical delay** between audio captured from a PipeWire sink and audio output on a Chromecast device is approximately **3 seconds**.

This delay is primarily due to how Chromecast handles **HLS streaming**, which includes:

1. **Buffering**: Chromecast typically buffers **3 full segments** before beginning playback.

2. **Playlist polling interval**: The device refreshes the playlist based on the `#EXT-X-TARGETDURATION` value, which defines the **expected segment duration** and how frequently the `.m3u8` file is reloaded.

3. **Segment duration limitations**: While the [Google Chromecast documentation](https://developers.google.com/cast/docs/media/streaming_protocols#http_live_streaming_hls) states:

   > `#EXT-X-TARGETDURATION` — How long in seconds each segment is.
   > This also determines how often we download/refresh the playlist manifest for a live stream.
   > The Web Receiver Player does not support durations shorter than **0.1 seconds**.

   In practice, **Chromecast cannot reliably handle `#EXT-X-TARGETDURATION` values below 1 second**. Attempting to use smaller values (e.g., 0.25s) may result in playback stop.

4. **Comparison with VLC**: Media players like **VLC** can handle much shorter segment durations (e.g., 0.25s) and respond to playlist updates more aggressively, leading to **lower latency** compared to Chromecast.

---

### 💡 Summary

| Player       | Minimum stable segment duration | Behavior                       |
| ------------ | ------------------------------- | ------------------------------ |
| Chromecast   | \~1 second                      | Buffers 3 segments, \~3s delay |
| VLC / hls.js | 0.25–0.5 seconds                | Can start playback much faster |

## Known limitations

* Only the **first** Chromecast discovered is used. Open a PR to add a selector!
* Audio only. Support for dummy video tracks is stub‑bed out in `ffmpeg.py`.
* Tested on **Manjaro Linux** / **PipeWire 1.4.1**;
* 3+ seconds delay.

## Roadmap

* Configuration with envs:
   | Variable            | Default  | Description                     |
   | ------------------- | -------- | ------------------------------- |
   | `PCAST_BITRATE`     | `256k`   | AAC bitrate fed to FFmpeg       |
   | `PCAST_SAMPLE_RATE` | `48000`  | Sampling rate (Hz)              |
   | `PCAST_HLS_FORMAT`  | `mpegts` | `mpegts` or experimental `fmp4` |
   | `PCAST_SINK_NAME`   | `P-Cast` | Name of the null sink           |

   See `config.py` for details.

* Multiple chromecast devices support.
* Package with `uvx` (`uv tool`).
* Enhance repository structure.
* Qt tray app to control chromecast device (pause/play).

## License

[GPL-3.0](LICENSE)