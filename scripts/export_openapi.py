"""导出公开与私有 OpenAPI Artifact；--check 同时校验两个入库文件。"""

import json
import sys
from pathlib import Path

from control_plane.app.bootstrap.app import create_app
from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app

OUT = Path(__file__).resolve().parents[1] / "openapi.json"
SANDBOX_OUT = Path(__file__).resolve().parents[1] / "sandbox-openapi.json"


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


def _artifacts() -> tuple[tuple[Path, bytes], ...]:
    return (
        (OUT, render().encode("utf-8")),
        (SANDBOX_OUT, render_sandbox().encode("utf-8")),
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
        print("openapi.json 与 sandbox-openapi.json 均与代码一致")
        return 0
    for path, content in artifacts:
        path.write_bytes(content)
    print(f"OpenAPI Artifacts 已导出（version={create_app().version}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
