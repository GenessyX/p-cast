import subprocess


def get_default_sink() -> str:
    result = subprocess.run(  # noqa: S603
        ["pactl", "get-default-sink"],  # noqa: S607
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
    )
    return result.stdout.strip()
