from dataclasses import dataclass
from typing import Literal

from p_cast.exceptions import StreamError

type AudioCodec = Literal["aac"]
type HlsSegmentType = Literal["mpegts", "fmp4"]

sample_rates: dict[AudioCodec, list[int]] = {
    "aac": [
        96000,
        88200,
        64000,
        48000,
        44100,
        32000,
        24000,
        22050,
        16000,
        12000,
        11025,
        8000,
        7350,
    ],
}


@dataclass
class StreamConfig:
    acodec: AudioCodec = "aac"
    bitrate: str = "192k"
    sampling_frequency: int = 48000
    hls_segment_type: HlsSegmentType = "mpegts"

    def __post_init__(self) -> None:
        if self.sampling_frequency not in sample_rates.get(self.acodec, []):
            msg = "Invalid sampling rate"
            raise StreamError(msg)

    @property
    def chromecast_hls_segment_type(self) -> str | None:
        match self.hls_segment_type:
            case "mpegts":
                return "ts_aac"
            case "fmp4":
                return "fmp4"
