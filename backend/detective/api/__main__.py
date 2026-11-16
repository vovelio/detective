"""Local single-process API runner with bounded WebSocket transport buffers."""

import argparse

import uvicorn

from detective.api.config import Settings
from detective.realtime.protocol import Limits


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    limits = Limits()
    settings = Settings.from_env()
    uvicorn.run(
        "detective.api.app:create_app",
        factory=True,
        host=args.host,
        proxy_headers=bool(settings.forwarded_allow_ips),
        forwarded_allow_ips=",".join(settings.forwarded_allow_ips),
        port=args.port,
        access_log=False,
        log_level="warning",
        ws="websockets",
        ws_max_size=limits.input_bytes,
        ws_max_queue=8,
        ws_per_message_deflate=False,
        ws_ping_interval=15,
        ws_ping_timeout=30,
    )


if __name__ == "__main__":
    main()
