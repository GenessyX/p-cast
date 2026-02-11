# P-Cast

**Cast anything you can hear on your Linux desktop to any Chromecast‑compatible speaker, TV or Nest device — live, with \~3 s latency.**

P‑Cast captures audio directly from PipeWire / PulseAudio, encodes it with FFmpeg and exposes the result as an HLS live stream that is played by Chromecast.

## Quick start

- Install p-cast with [uv](https://docs.astral.sh/uv/getting-started/installation/), and run it as needed:

    ```bash
    uv tool install p-cast
    p-cast
    ```

- Run once to test (ephemeral install):
    - using `uvx`:
        ```bash
        uvx p-cast
        ```
    - Using `pipx`:
        ```bash
        pipx run p-cast
        ```

- Run from source code using `uv`:

    ```bash
    git clone https://github.com/GenessyX/p-cast
    cd p-cast

    uv run python p_cast
    ```

    or alternatively with `pip`,

    ```bash
    git clone https://github.com/GenessyX/p-cast
    cd p-cast
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    python p_cast
    ```

**THEN** while running, under Sound Settings, simply choose the Chromecast device of your choice (set it as default sink or route an app explicitly). Audio will be cast to your device.

## Features

* **Virtual sinks** - creates virtual null sink for each Chromecast device
* **Automatic device discovery** – finds all Chromecast devices or speaker groups on your LAN via mDNS.
* **On‑the‑fly transcoding** – AAC @ 256 kbps by default (this is the best available codec; bit rate customizable)
* **Live HLS** – 0.5 s segments; Chromecast buffers 3 -> \~3 s end‑to‑end delay.
* **Volume follow** – changes to the PipeWire sink volume are synced to the Chromecast.
* **Tiny footprint** – single Python process + FFmpeg child; no browser or GUI required.

## Command line arguments:

    options:
        -h, --help             show help message and exit
        -b, --bitrate BITRATE  audio bitrate (default: 256k)
        -p, --port PORT        streaming server tcp port (default: 3000)
        --ffmpeg FFMPEG        path to ffmpeg binary supporting 'pulse' format (default: ffmpeg)
        --log-level {DEBUG,INFO,WARNING,ERROR}
                               logging level (default: INFO)

## Requirements

| Component         | Purpose                     |
| ----------------- | --------------------------- |
| Linux w/ PipeWire | audio capture               |
| Python ≥ 3.12     | application runtime         |
| FFmpeg ≥ 6.1 w/ pulse support | encoding & HLS muxing  |
| Chromecast        | playback device on same LAN |

Dependencies are declared in **pyproject.toml**. The examples below use [**uv**](https://github.com/astral-sh/uv) but regular `pip` works just as well.

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

## Optional: prevent desktop notifications or other sounds from being cast

Use `pavucontrol` or another advanced volume controller to choose Playback to your Chromecast device only from your desired sound source (eg, the music/video player), while leaving something else as the default output device.

## How it works (TL;DR)

1. main app starts [Granian](https://github.com/emmett-framework/granian) and serves the Starlette app on default port **:3000**.
2. `app.py`
   1. discovers a Chromecast (`cast.find_chromecast`),
   2. creates a **null sink** named `P‑Cast` (`device.SinkController`),
   3. launches FFmpeg (`ffmpeg.create_ffmpeg_stream_command`) to read from `P‑Cast.monitor` and write HLS segments to a temp dir,
   4. mounts that dir at **/stream**.
3. After 2 s the Chromecast receives `http://<host>:<port>/stream/index.m3u8` via the Media Controller and starts buffering. (port defaults to 3000)
4. A background task listens for `pactl` volume‑change events and calls `Chromecast.set_volume(...)`.

Everything runs in a single Python process; FFmpeg is the only external binary. The program remains in foreground, but can be backgrounded with `&`.

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

4. **Comparison with VLC**: Local media players like **VLC** can handle much shorter segment durations (e.g., 0.25s) and respond to playlist updates more aggressively, leading to **lower latency** compared to Chromecast.

## Known limitations

* Audio only. Support for dummy video tracks is stub‑bed out in `ffmpeg.py`.
* Tested on **Manjaro Linux** / **PipeWire 1.4.1** and ***Linux Mint 22.3 (ubuntu 24.04) *** / ***PipeWire 1.0.5***
* ~3 seconds delay (this is essentially a chromecast limitation)
* Only one chromecast may be streamed to at a time. You can make use of Google Home to create Speaker Groups for synced playback to multiple chromecasts (each group will become a selectable audio sink)

## Roadmap

* Configuration with envs:
   | Variable            | Default  | Description                     |
   | ------------------- | -------- | ------------------------------- |
   | `PCAST_BITRATE`     | `256k`   | AAC bitrate fed to FFmpeg       |
   | `PCAST_SAMPLE_RATE` | `48000`  | Override Sampling rate (Hz)     |
   | `PCAST_PORT`        | `3000`   | Port used for streaming to chromecast |

    * currently environment variables are used internally but not exposed to the user (you can use the command line however)

* Enhance repository structure.
* Qt tray app to control chromecast device (pause/play).

## License

[GPL-3.0](LICENSE)