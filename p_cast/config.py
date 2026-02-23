from dataclasses import dataclass
from typing import Literal

type AudioCodec = Literal["aac"]
type HlsSegmentType = Literal["mpegts", "fmp4"]

MAX_SAMPLE_RATE = 96000


@dataclass
class StreamConfig:
    """Audio encoding and HLS output parameters for the FFmpeg streaming pipeline."""

    # aac (-lc) is the only supported codec, and there is none better.
    # Chromecast won't support FLAC w/ HLS. MP3 is inferior quality per bitrate and
    # not significantly less resource intensive to encode
    acodec: AudioCodec = "aac"
    bitrate: str = "192k"
    hls_segment_type: HlsSegmentType = "mpegts"
    ffmpeg_bin: str = "ffmpeg"

    @property
    def chromecast_hls_segment_type(self) -> str | None:
        match self.hls_segment_type:
            case "mpegts":
                return "ts_aac"
            case "fmp4":
                return "fmp4"

    @property
    def content_type(self) -> str:
        return "application/vnd.apple.mpegurl"
