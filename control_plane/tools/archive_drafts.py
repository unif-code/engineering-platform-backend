from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import TextIO

from sqlalchemy import Engine

from control_plane.app.modules.configuration import (
    ConfigurationDependencies,
    archive_stale_drafts,
)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="Archive inactive configuration drafts.")


def _runtime() -> tuple[Engine, ConfigurationDependencies]:
    from control_plane.app.bootstrap.app import (
        configuration_dependencies,
        configuration_runtime_engine,
    )

    return configuration_runtime_engine(), configuration_dependencies()


def main(
    argv: list[str] | None = None,
    *,
    engine: Engine | None = None,
    dependencies: ConfigurationDependencies | None = None,
    archive: Callable[..., int] = archive_stale_drafts,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    _parser().parse_args(argv)
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    if engine is None or dependencies is None:
        runtime_engine, runtime_dependencies = _runtime()
        engine = engine or runtime_engine
        dependencies = dependencies or runtime_dependencies
    try:
        with engine.begin() as db:
            archived = archive(
                db,
                now=dependencies.clock.now(),
                dependencies=dependencies,
            )
    except Exception:
        errors.write(json.dumps({"status": "FAILED"}, separators=(",", ":")) + "\n")
        return 1
    output.write(json.dumps({"archivedDrafts": archived}, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
