import json
import subprocess
import sys
from pathlib import Path

import pytest

from control_plane.app import __version__
from control_plane.app.bootstrap.app import create_app
from control_plane.app.bootstrap.source_control_connector import (
    create_source_control_connector_app,
)
from scripts import export_openapi
from scripts.export_openapi import render


def test_render_is_deterministic_and_versioned() -> None:
    first, second = render(), render()
    assert first == second
    assert f'"version": "{__version__}"' in first
    assert json.loads(first)["info"]["version"] == "0.3.0"


def test_render_contains_the_typed_requirement_contract() -> None:
    schema = json.loads(render())
    requirement = schema["components"]["schemas"]["RequirementResponseDto"]

    assert requirement["properties"]["workspaceId"]["format"] == "uuid"
    assert requirement["properties"]["state"] == {"$ref": "#/components/schemas/RequirementState"}
    assert requirement["properties"]["createdAt"]["format"] == "date-time"
    requirement_paths = {
        path for path in schema["paths"] if path.startswith("/api/v1/requirements")
    }
    assert requirement_paths == {
        "/api/v1/requirements",
        "/api/v1/requirements/{requirementId}",
    }
    repository_path = schema["paths"]["/api/v1/workspaces/{workspaceId}/repositories"]["get"]
    assert repository_path["operationId"] == "source_control_authorized_repositories_list"
    properties = schema["components"]["schemas"]["AuthorizedRepositoryResponseDto"]["properties"]
    assert set(properties) == {
        "repositoryId",
        "provider",
        "projectPath",
        "defaultBranch",
    }


def test_render_does_not_publish_future_requirement_delivery_contract() -> None:
    schema = json.loads(render())
    components = schema["components"]["schemas"]
    assert "WorkItemDeliveryResponseDto" not in components
    assert set(components["WorkItemResponseDto"]["properties"]).isdisjoint(
        {
            "integrationDeliveryState",
            "integrationMergeRequestBindingId",
            "integrationBlockedReasonCode",
            "integrationUpdatedAt",
        }
    )


def test_source_control_webhook_is_connector_only() -> None:
    public_schema = create_app().openapi()
    public_paths = set(public_schema["paths"])
    connector = create_source_control_connector_app()
    connector_paths = set(connector.openapi()["paths"])

    assert not any(path.startswith("/webhooks/gitlab/") for path in public_paths)
    assert public_schema["components"]["schemas"]["RepositoryBindingBlockedReason"]["enum"] == [
        "CONNECTOR_UNAVAILABLE",
        "REPOSITORY_NOT_FOUND",
        "ACCESS_DENIED",
        "POLICY_DENIED",
        "BINDING_CONFLICT",
        "OWNER_UNASSIGNED",
        "OWNER_INELIGIBLE",
        "REPOSITORY_NOT_AUTHORIZED",
        "RECONCILIATION_PENDING",
    ]
    assert connector_paths == {"/healthz", "/readyz", "/webhooks/gitlab/{repository_id}"}


# 真正的守门用例：不先导出，直接校验入库件。
# 任何改了路由/DTO 却没重新导出 openapi.json 的提交都会在这里失败。
# 本文件没有任何用例写入库件（写模式一律落到 tmp_path），因此不存在执行顺序依赖；
# 跑完测试也不会把过期件"修好"，CI 末尾那道独立的 --check 才真正有牙。
def test_committed_artifact_matches_code() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/export_openapi.py", "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# 写模式 → --check 的正向闭环：导出的内容必须能自洽通过校验。全程只碰 tmp_path。
def test_check_mode_passes_after_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = export_openapi.OUT
    before = committed.read_bytes()

    artifact = tmp_path / "openapi.json"
    monkeypatch.setattr(export_openapi, "OUT", artifact)

    monkeypatch.setattr(sys, "argv", ["export_openapi.py"])
    assert export_openapi.main() == 0
    assert artifact.read_text(encoding="utf-8") == render()

    monkeypatch.setattr(sys, "argv", ["export_openapi.py", "--check"])
    assert export_openapi.main() == 0

    assert committed.read_bytes() == before  # 写模式绝不碰入库件


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


def test_check_rejects_semantically_equal_crlf_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = tmp_path / "openapi.json"
    crlf_content = render().replace("\n", "\r\n")
    artifact.write_bytes(crlf_content.encode("utf-8"))
    assert json.loads(artifact.read_bytes()) == json.loads(render())

    monkeypatch.setattr(export_openapi, "OUT", artifact)
    monkeypatch.setattr(sys, "argv", ["export_openapi.py", "--check"])

    assert export_openapi.main() == 1
    assert "不一致" in capsys.readouterr().err
