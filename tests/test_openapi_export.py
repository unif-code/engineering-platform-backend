import subprocess
import sys
from pathlib import Path

import pytest

from control_plane.app import __version__
from scripts import export_openapi
from scripts.export_openapi import render


def test_render_is_deterministic_and_versioned() -> None:
    first, second = render(), render()
    assert first == second
    assert f'"version": "{__version__}"' in first


# 真正的守门用例：不先导出，直接校验入库件。
# 任何改了路由/DTO 却没重新导出 openapi.json 的提交都会在这里失败。
# 必须排在 test_check_mode_passes_after_export 之前——后者会先导出，把过期件"修好"。
def test_committed_artifact_matches_code() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/export_openapi.py", "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_check_mode_passes_after_export() -> None:
    subprocess.run([sys.executable, "scripts/export_openapi.py"], check=True)
    result = subprocess.run([sys.executable, "scripts/export_openapi.py", "--check"])
    assert result.returncode == 0


# 退出码 1 的两条分支（内容不一致 / 文件缺失）都在进程内验证，全程只碰 tmp_path，不动仓库文件。
def test_check_detects_tampered_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = render()
    tampered = original.replace("Successful Response", "Successfxl Response", 1)
    assert tampered != original, "前置条件：锚点文本必须存在，否则本用例没有真正篡改"

    artifact = tmp_path / "openapi.json"
    artifact.write_text(tampered, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["export_openapi.py", "--check"])
    monkeypatch.setattr(export_openapi, "OUT", artifact)
    assert export_openapi.main() == 1
    assert "不一致" in capsys.readouterr().err

    monkeypatch.setattr(export_openapi, "OUT", tmp_path / "missing.json")
    assert export_openapi.main() == 1
    assert "不一致" in capsys.readouterr().err
    assert artifact.read_text(encoding="utf-8") == tampered  # --check 绝不写文件
