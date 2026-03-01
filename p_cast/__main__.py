import argparse
import fcntl
import os
import sys
from pathlib import Path

from granian.constants import Interfaces, Loops
from granian.log import LogLevels
from granian.server import Server

# Held open for the lifetime of the process; closing it releases the lock.
_instance_lock = None


def _acquire_instance_lock() -> None:
    """Acquire an exclusive lock so only one p-cast instance can run per user.

    Uses a lock file in /tmp rather than checking process lists, so the lock
    is released automatically by the OS even if the process is killed or crashes.
    """
    global _instance_lock  # noqa: PLW0603
    lock_path = f"/tmp/p-cast-{os.getuid()}.lock"  # noqa: S108
    try:
        _instance_lock = Path(lock_path).open("w")  # noqa: SIM115
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write("error: another instance of p-cast is already running\n")
        sys.exit(1)


def _daemonize(pid_file: Path | None) -> None:
    """Double-fork daemonize: detach from controlling terminal and run in background.

    After this call the process has no controlling terminal, is in its own
    session, and stdin/stdout/stderr are redirected to /dev/null.
    """
    # First fork: child is not a process-group leader, so setsid() will succeed.
    if os.fork() > 0:
        os._exit(0)
    # Become session leader — detaches from the controlling terminal.
    os.setsid()
    # Second fork: daemon can never re-acquire a controlling terminal.
    if os.fork() > 0:
        os._exit(0)
    if pid_file:
        with pid_file.open("w") as f:
            f.write(f"{os.getpid()}\n")
    # point std streams of daemon to /dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    # close devnull, except in the edge case where the std streams had been previously
    # closed and so devnull took their standard file descriptor
    if devnull > 2:  # noqa: PLR2004
        os.close(devnull)


_GRANIAN_LOG_LEVELS = {
    "DEBUG": LogLevels.debug,
    "INFO": LogLevels.info,
    "WARNING": LogLevels.warning,
    "ERROR": LogLevels.error,
}

MAX_AAC_BITRATE = 320


def bitrate_ok(bitrate: str) -> bool:
    if bitrate[-1] != "k":
        return False

    bitrate = bitrate[:-1]

    return str.isnumeric(bitrate) and float(int(bitrate)) == float(bitrate) and int(bitrate) <= MAX_AAC_BITRATE


def main() -> None:
    parser = argparse.ArgumentParser(description="Cast audio from PipeWire to Chromecast")
    parser.add_argument("-b", "--bitrate", default="256k", help="audio bitrate (default: 256k)")
    parser.add_argument("-p", "--port", type=int, default=3000, help="streaming server tcp port (default: 3000)")
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="path to ffmpeg binary supporting 'pulse' format (default: ffmpeg)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging level (default: INFO)",
    )
    parser.add_argument("--log-file", default="console", help="write logs to this filename instead of to the console")
    parser.add_argument(
        "--daemonize",
        action="store_true",
        help="detach from terminal and run as a background daemon (requires --log-file)",
    )
    parser.add_argument("--pid-file", default=None, metavar="PATH", help="write PID to this file on startup")
    args = parser.parse_args()

    if not bitrate_ok(args.bitrate):
        sys.stderr.write(f"Provided bitrate is invalid or above {MAX_AAC_BITRATE}k: {args.bitrate}\n")
        sys.exit(1)

    if args.daemonize and args.log_file == "console":
        sys.stderr.write("error: --daemonize requires --log-file\n")
        sys.exit(1)

    _acquire_instance_lock()

    pid_file = None
    if args.pid_file:
        pid_file = Path(args.pid_file)
        with pid_file.open("w") as f:
            f.write(f"{os.getpid()}\n")  # will be overwritten if we daemonize

    if args.daemonize:
        _daemonize(pid_file)

    os.environ["PCAST_BITRATE"] = args.bitrate
    os.environ["PCAST_PORT"] = str(args.port)
    os.environ["PCAST_FFMPEG"] = args.ffmpeg
    os.environ["PCAST_LOG_LEVEL"] = args.log_level
    if args.log_file != "console":
        os.environ["PCAST_LOG_FILE"] = args.log_file

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

    if pid_file:
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
