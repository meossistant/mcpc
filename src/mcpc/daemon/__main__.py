"""`python -m mcpc.daemon`: run the background daemon in the foreground of this process."""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Sequence
from functools import partial

import anyio

from mcpc.daemon.server import serve


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the daemon until it exits."""
    parser = argparse.ArgumentParser(prog="mcpc.daemon", description="mcpc background daemon")
    parser.add_argument("--server-idle-minutes", type=float, default=15.0)
    parser.add_argument("--daemon-idle-minutes", type=float, default=60.0)
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(partial(serve, args.server_idle_minutes, args.daemon_idle_minutes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
