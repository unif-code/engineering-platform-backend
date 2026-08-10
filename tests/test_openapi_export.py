import subprocess
import sys

from control_plane.app import __version__
from scripts.export_openapi import render


def test_render_is_deterministic_and_versioned() -> None:
    first, second = render(), render()
    assert first == second
    assert f'"version": "{__version__}"' in first


def test_check_mode_passes_after_export() -> None:
    subprocess.run([sys.executable, "scripts/export_openapi.py"], check=True)
    result = subprocess.run([sys.executable, "scripts/export_openapi.py", "--check"])
    assert result.returncode == 0
