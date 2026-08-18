"""导出 OpenAPI Artifact：默认写 openapi.json；--check 校验入库文件与代码一致。"""

import json
import sys
from pathlib import Path

from control_plane.app.bootstrap.app import create_app

OUT = Path(__file__).resolve().parents[1] / "openapi.json"


def render() -> str:
    return json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    content = render().encode("utf-8")
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_bytes() != content:
            print(
                "openapi.json 与代码不一致：运行 uv run python scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print("openapi.json 与代码一致")
        return 0
    OUT.write_bytes(content)
    print(f"openapi.json 已导出（version={create_app().version}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
