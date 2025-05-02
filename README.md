# P-Cast

Casts local audio-device to chromecast compatible device.

```sh
uv run python run.py
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
