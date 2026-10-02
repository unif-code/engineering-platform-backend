"""导出公开/私有 OpenAPI 与 Model 来源清单 Schema；--check 校验所有入库构件。"""

import json
import sys
from pathlib import Path

from control_plane.app.bootstrap.app import create_app
from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app
from control_plane.app.modules.model_gateway.adapters.material_sources import MaterialSourceManifest

OUT = Path(__file__).resolve().parents[1] / "openapi.json"
SANDBOX_OUT = Path(__file__).resolve().parents[1] / "sandbox-openapi.json"
MATERIAL_SOURCES_OUT = OUT.parent / "model-material-sources.schema.json"


def render() -> str:
    return json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_sandbox() -> str:
    return (
        json.dumps(
            create_sandbox_controller_app().openapi(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_material_sources() -> str:
    return (
        json.dumps(
            MaterialSourceManifest.model_json_schema(by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _artifacts() -> tuple[tuple[Path, bytes], ...]:
    return (
        (OUT, render().encode("utf-8")),
        (SANDBOX_OUT, render_sandbox().encode("utf-8")),
        (MATERIAL_SOURCES_OUT, render_material_sources().encode("utf-8")),
    )


def main() -> int:
    artifacts = _artifacts()
    if "--check" in sys.argv:
        mismatched = [
            path.name
            for path, content in artifacts
            if not path.exists() or path.read_bytes() != content
        ]
        if mismatched:
            print(
                f"{', '.join(mismatched)} 与代码不一致："
                "运行 uv run python scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print("OpenAPI 与 Model 来源清单 Schema 均与代码一致")
        return 0
    for path, content in artifacts:
        path.write_bytes(content)
    print(f"OpenAPI Artifacts 已导出（version={create_app().version}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
