from p_cast.config import StreamConfig

# note, the ffmpeg executable is required, and it must support the pulse input format (--enable).
# check with `ffmpeg -formats | grep pulse`.


def create_ffmpeg_stream_command(
    sink: str,
    stream_dir: str,
    sample_rate: int | None = None,
    config: StreamConfig | None = None,
) -> list[str]:
    if config is None:
        config = StreamConfig()

    acodec = []
    if config.acodec == "aac":
        acodec = ["-c:a", "aac", "-profile:a", "aac_low"]

    # resampling is OPTIONAL
    resample = ["-ar", str(sample_rate)] if sample_rate is not None else []

    return [
        config.ffmpeg_bin,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "pulse",
        # Buffer ~340ms of input audio (16 x ~21ms AAC frames at 48 kHz) so that brief
        # PipeWire stalls don't stall the encoder and starve the HLS segment output.
        # This is an input-side queue and has no effect on output latency.
        "-thread_queue_size",
        "16",
        "-i",
        sink,
        ## BLACK SQUARE --
        # "-map",
        # "0:v",
        # "-map",
        # "1:a",
        # "-f",
        # "lavfi",
        # "-re",
        # "-i",
        # "color=size=2x2:rate=50:color=black",
        # "-c:v",
        # "libx264",
        # "-preset",
        # "ultrafast",
        # "-tune",
        # "zerolatency",
        # "-b:v",
        # "48k",
        # "-pix_fmt",
        # "yuv420p",
        # "-profile:v",
        # "baseline",
        # "-level",
        # "3.0",
        # "-g",
        # "25",
        # "-keyint_min",
        # "25",
        # "-sc_threshold",
        # "0",
        ## BLACK SQUARE --
        ## COPY --
        # "-c:v",
        # "copy",
        ## COPY --
        *acodec,
        "-b:a",
        config.bitrate,
        *resample,
        "-fflags",
        "+nobuffer+discardcorrupt",
        "-flags",
        "low_delay",
        "-flush_packets",
        "1",
        "-max_delay",
        "0",
        "-muxdelay",
        "0",
        "-strict",
        "experimental",
        "-avioflags",
        "direct",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-f",
        "hls",
        "-hls_time",
        "0.5",
        "-hls_list_size",
        "6",
        "-hls_delete_threshold",
        "1",
        "-hls_flags",
        "split_by_time+program_date_time+independent_segments+append_list",
        "-hls_segment_type",
        config.hls_segment_type,
        "-master_pl_name",
        "index.m3u8",
        "-var_stream_map",
        "a:0",
        stream_dir + "/main_stream.m3u8",
    ]
