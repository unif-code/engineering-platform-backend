import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_image_bootstrap_command_uses_the_bundled_python_cli() -> None:
    """Dropping the copied venv/PATH or documenting a uv-only command must fail."""
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime_stage = dockerfile.rsplit("\nFROM ", maxsplit=1)[1]
    assert "COPY --from=builder --chown=app:app /app /app" in runtime_stage
    assert 'ENV PATH="/app/.venv/bin:$PATH"' in runtime_stage

    runbook = (REPOSITORY_ROOT / "docs" / "runbook-break-glass.md").read_text(encoding="utf-8")
    bootstrap_section = runbook.split("## 一次性 bootstrap", maxsplit=1)[1].split(
        "## break-glass recovery", maxsplit=1
    )[0]
    assert (
        "python -m control_plane.tools.bootstrap_admin "
        "--employee-no 00000000 --display-name 平台超级管理员" in bootstrap_section
    )
    assert "uv run" not in bootstrap_section

    result = subprocess.run(
        [sys.executable, "-m", "control_plane.tools.bootstrap_admin", "--help"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--employee-no" in result.stdout
    assert "--display-name" in result.stdout
