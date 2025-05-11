from pathlib import Path

from granian.constants import Interfaces, Loops
from granian.log import LogLevels
from granian.server import Server

if __name__ == "__main__":
    ssl_cert = Path(__file__).parent / "cert.pem"
    ssl_key = Path(__file__).parent / "key.pem"
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
