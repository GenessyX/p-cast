from dataclasses import dataclass
from enum import Enum
from typing import Literal

from exceptions import StreamError

type AudioCodec = Literal["aac", "libopus"]

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
    "libopus": [48000, 24000, 16000, 12000, 8000],
}


class HlsSegmentType(str, Enum):
    MPEG_TS = "mpegts"
    FMP4 = "fmp4"

    @property
    def chromecast_format(self) -> str | None:
        match self:
            case HlsSegmentType.MPEG_TS:
                return None
            case HlsSegmentType.FMP4:
                return "fmp4"


@dataclass
class StreamConfig:
    acodec: AudioCodec = "aac"
    bitrate: str = "192k"
    sampling_frequency: int = 48000
    hls_segment_type: HlsSegmentType = HlsSegmentType.MPEG_TS

    def __post_init__(self) -> None:
        if self.sampling_frequency not in sample_rates.get(self.acodec, []):
            msg = "Invalid sampling rate"
            raise StreamError(msg)
