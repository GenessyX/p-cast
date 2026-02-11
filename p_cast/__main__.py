import argparse
import os
import sys

from granian.constants import Interfaces, Loops
from granian.log import LogLevels
from granian.server import Server

_GRANIAN_LOG_LEVELS = {
    "DEBUG": LogLevels.debug,
    "INFO": LogLevels.info,
    "WARNING": LogLevels.warning,
    "ERROR": LogLevels.error,
}

def bitrate_ok(bitrate: str) -> bool:
    if bitrate[-1] != "k":
        return False

    bitrate = bitrate[:-1]

    return str.isnumeric(bitrate) and float(int(bitrate)) == float(bitrate) and int(bitrate) <= 320


def main() -> None:
    parser = argparse.ArgumentParser(description="Cast audio from PipeWire to Chromecast")
    parser.add_argument("-b", "--bitrate", default="256k", help="audio bitrate (default: 256k)")
    parser.add_argument("-p", "--port", type=int, default=3000, help="streaming server TCP port (default: 3000)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="path to ffmpeg binary (default: ffmpeg)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging level (default: INFO)",
    )
    args = parser.parse_args()

    if not bitrate_ok(args.bitrate):
        print("Provided bitrate is invalid or above 320k: ", args.bitrate)
        sys.exit(1)

    os.environ["P_CAST_BITRATE"] = args.bitrate
    os.environ["P_CAST_PORT"] = str(args.port)
    os.environ["P_CAST_FFMPEG"] = args.ffmpeg
    os.environ["P_CAST_LOG_LEVEL"] = args.log_level

    server = Server(
        target="p_cast.app:create_app",
        factory=True,
        address="0.0.0.0",  # noqa: S104
        port=args.port,
        interface=Interfaces.ASGI,
        loop=Loops.uvloop,
        log_level=_GRANIAN_LOG_LEVELS.get(args.log_level, LogLevels.info),
    )
    server.serve()


if __name__ == "__main__":
    main()
