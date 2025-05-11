from granian.constants import Interfaces, Loops
from granian.log import LogLevels
from granian.server import Server


def main() -> None:
    server = Server(
        target="p_cast.app:create_app",
        factory=True,
        address="0.0.0.0",  # noqa: S104
        port=3000,
        interface=Interfaces.ASGI,
        loop=Loops.uvloop,
        log_level=LogLevels.debug,
    )
    server.serve()


if __name__ == "__main__":
    main()
