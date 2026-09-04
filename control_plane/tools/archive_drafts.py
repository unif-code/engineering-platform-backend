from __future__ import annotations

import argparse
import json
import sys
from typing import TextIO

from control_plane.app.modules.configuration import (
    ConfigurationDependencies,
    PolicyRuntimeRegistry,
)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="Archive inactive configuration drafts.")


def _runtime() -> tuple[PolicyRuntimeRegistry, ConfigurationDependencies]:
    from control_plane.app.bootstrap.app import (
        configuration_http_runtime,
    )

    runtime = configuration_http_runtime()
    return runtime.owners, runtime.dependencies


def main(
    argv: list[str] | None = None,
    *,
    owners: PolicyRuntimeRegistry | None = None,
    dependencies: ConfigurationDependencies | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    _parser().parse_args(argv)
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    try:
        if owners is None or dependencies is None:
            runtime_owners, runtime_dependencies = _runtime()
            owners = owners or runtime_owners
            dependencies = dependencies or runtime_dependencies
        archived = 0
        failed = False
        for namespace in ("identity", "requirement.gate"):
            try:
                archived += owners.resolve(namespace).archive(now=dependencies.clock.now())
            except Exception:
                failed = True
        if failed:
            raise RuntimeError("Policy owner archival failed")
    except Exception:
        errors.write(json.dumps({"status": "FAILED"}, separators=(",", ":")) + "\n")
        return 1
    output.write(json.dumps({"archivedDrafts": archived}, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
