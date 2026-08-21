from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

from control_plane.tools import bootstrap_admin

_DEFAULT_CLOSE_AFTER_SECONDS = 180


def _positive_seconds(value: str) -> int:
    seconds = int(value)
    if seconds < 1 or seconds > 900:
        raise argparse.ArgumentTypeError("close delay must be between 1 and 900 seconds")
    return seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run local Super Admin bootstrap in a short-lived interactive window."
    )
    parser.add_argument("--employee-no", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument(
        "--close-after-seconds",
        default=_DEFAULT_CLOSE_AFTER_SECONDS,
        type=_positive_seconds,
    )
    return parser


def _wait_for_enter_or_timeout(seconds: float) -> None:
    if os.name == "nt" and sys.stdin.isatty():
        import msvcrt

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if msvcrt.kbhit() and msvcrt.getwch() in {"\r", "\n"}:
                return
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return

    entered = threading.Event()

    def wait_for_input() -> None:
        try:
            sys.stdin.readline()
        finally:
            entered.set()

    threading.Thread(target=wait_for_input, daemon=True).start()
    entered.wait(timeout=seconds)


BootstrapMain = Callable[..., int]
WaitForClose = Callable[[float], None]


def main(
    argv: list[str] | None = None,
    *,
    bootstrap: BootstrapMain = bootstrap_admin.main,
    sleeper: WaitForClose = _wait_for_enter_or_timeout,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    evidence = stderr if stderr is not None else sys.stderr
    exit_code = bootstrap(
        [
            "--employee-no",
            args.employee_no,
            "--display-name",
            args.display_name,
            "--interactive",
        ],
        stdout=output,
        stderr=evidence,
    )
    if exit_code != 0:
        return exit_code

    evidence.write(
        json.dumps(
            {
                "event": "super_admin_bootstrap_window",
                "result": "AUTO_CLOSE_PENDING",
                "windowCloseAfterSeconds": args.close_after_seconds,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    evidence.flush()
    sleeper(args.close_after_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
