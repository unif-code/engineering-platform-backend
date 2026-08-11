"""Guard that import-linter contracts cover every domain module."""

import tomllib
from pathlib import Path
from typing import TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]
MODULES_DIR = ROOT / "control_plane" / "app" / "modules"
MODULE_PREFIX = "control_plane.app.modules"
DEEP_LAYERS = ("api", "application", "domain", "ports", "adapters")


class ImportLinterContract(TypedDict, total=False):
    containers: list[str]
    forbidden_modules: list[str]
    source_modules: list[str]
    type: str


def actual_modules() -> set[str]:
    return {
        path.name
        for path in MODULES_DIR.iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    }


def contracts() -> list[ImportLinterContract]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return cast(list[ImportLinterContract], data["tool"]["importlinter"]["contracts"])


def test_layer_contract_covers_all_modules() -> None:
    layers = next(contract for contract in contracts() if contract["type"] == "layers")
    declared = {container.rsplit(".", 1)[-1] for container in layers["containers"]}
    assert declared == actual_modules(), (
        f"import-linter layers contract is missing modules: {actual_modules() - declared}"
    )


def test_facade_contracts_are_symmetric() -> None:
    modules = actual_modules()
    expected = {
        f"{MODULE_PREFIX}.{source}": {
            f"{MODULE_PREFIX}.{target}.{layer}"
            for target in modules - {source}
            for layer in DEEP_LAYERS
        }
        for source in modules
    }
    declared = {source: set[str]() for source in expected}

    for contract in contracts():
        if contract["type"] != "forbidden":
            continue
        for source in contract["source_modules"]:
            if source in declared:
                declared[source].update(contract["forbidden_modules"])

    assert declared == expected
