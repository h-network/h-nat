#!/usr/bin/env python3
"""Block until a TCP connection to Redis succeeds, or time out.

GitHub Actions' service-container health check (Docker's own HEALTHCHECK
protocol, evaluated inside the container) gates when the job's steps start,
but that's not the same guarantee as "the host-side port mapping is actually
accepting connections at this exact moment" -- under load, the two can be
seconds apart. This is the defensive check for that gap, run once right
before a module's tests that actually need Redis.

    ./ci/scripts/wait_for_redis.py [host] [port] [--timeout SECONDS]
"""
from __future__ import annotations

import argparse
import socket
import sys
import time


def wait_for_redis(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    print(f"Redis at {host}:{port} never became reachable within {timeout}s: {last_error}", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=6379)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    if wait_for_redis(args.host, args.port, args.timeout):
        print(f"Redis at {args.host}:{args.port} is reachable")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
