from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import TextIO


def _bootstrap_success(
    calls: list[list[str]],
) -> Callable[..., int]:
    def invoke(
        argv: list[str],
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> int:
        calls.append(argv)
        assert stdout is not None
        assert stderr is not None
        stdout.write("temporary-secret\n")
        stderr.write('{"event":"super_admin_bootstrap","result":"SUCCESS"}\n')
        return 0

    return invoke


def test_success_keeps_the_dedicated_window_open_for_three_minutes() -> None:
    from control_plane.tools.bootstrap_admin_window import main

    calls: list[list[str]] = []
    delays: list[float] = []
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--employee-no", "00000000", "--display-name", "平台超级管理员"],
        bootstrap=_bootstrap_success(calls),
        sleeper=delays.append,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert calls == [
        [
            "--employee-no",
            "00000000",
            "--display-name",
            "平台超级管理员",
            "--interactive",
        ]
    ]
    assert delays == [180]
    assert stdout.getvalue() == "temporary-secret\n"
    lines = stderr.getvalue().splitlines()
    assert json.loads(lines[-1]) == {
        "event": "super_admin_bootstrap_window",
        "result": "AUTO_CLOSE_PENDING",
        "windowCloseAfterSeconds": 180,
    }
    assert "temporary-secret" not in stderr.getvalue()


def test_failed_bootstrap_returns_immediately_without_holding_window() -> None:
    from control_plane.tools.bootstrap_admin_window import main

    delays: list[float] = []

    exit_code = main(
        ["--employee-no", "00000000", "--display-name", "平台超级管理员"],
        bootstrap=lambda *_args, **_kwargs: 3,
        sleeper=delays.append,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 3
    assert delays == []


def test_close_delay_can_be_shortened_for_local_smoke_checks() -> None:
    from control_plane.tools.bootstrap_admin_window import main

    delays: list[float] = []

    exit_code = main(
        [
            "--employee-no",
            "00000000",
            "--display-name",
            "平台超级管理员",
            "--close-after-seconds",
            "2",
        ],
        bootstrap=_bootstrap_success([]),
        sleeper=delays.append,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert delays == [2]


def test_windows_launcher_is_ascii_for_windows_powershell_51() -> None:
    launcher = Path(__file__).parents[2] / "scripts" / "open-local-super-admin.ps1"

    launcher.read_bytes().decode("ascii")
