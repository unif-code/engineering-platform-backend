from __future__ import annotations

import argparse
import json
import sys
from typing import TextIO

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.source_control import (
    SourceControlDependencies,
    SourceControlError,
    WorkspaceRepositoryDto,
    list_authorized_repositories,
    register_workspace_repository,
    remove_workspace_repository,
)

_ACTOR = "SYSTEM:source-control-repository-tool"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage authorized Source Control repository metadata and Secret References.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register")
    register.add_argument("--repository-id", required=True)
    register.add_argument("--workspace-id", required=True)
    register.add_argument("--project-id", required=True)
    register.add_argument("--project-path", required=True)
    register.add_argument("--connection-ref", required=True)
    register.add_argument("--credential-secret-ref", required=True)
    register.add_argument("--webhook-signing-secret-ref")

    list_command = commands.add_parser("list")
    list_command.add_argument("--workspace-id", required=True)

    remove = commands.add_parser("remove")
    remove.add_argument("--repository-id", required=True)
    remove.add_argument("--expected-revision", required=True, type=int)
    return parser


def _runtime() -> tuple[Engine, SourceControlDependencies]:
    from control_plane.app.bootstrap.app import (
        source_control_dependencies,
        source_control_query_runtime_engine,
    )

    return source_control_query_runtime_engine(), source_control_dependencies()


def _registered_output(value: WorkspaceRepositoryDto) -> dict[str, object]:
    return {
        "repositoryId": value.repository_id,
        "workspaceId": value.workspace_id,
        "provider": value.provider,
        "projectPath": value.project_path,
        "defaultBranch": value.default_branch,
        "status": value.status.value,
        "revision": value.revision,
    }


def _write(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":")) + "\n")


def main(
    argv: list[str] | None = None,
    *,
    engine: Engine | None = None,
    dependencies: SourceControlDependencies | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    if engine is None or dependencies is None:
        runtime_engine, runtime_dependencies = _runtime()
        engine = engine or runtime_engine
        dependencies = dependencies or runtime_dependencies
    try:
        if args.command == "register":
            with engine.begin() as db:
                registered = register_workspace_repository(
                    dependencies.repository_factory(db),
                    repository_id=args.repository_id,
                    workspace_id=args.workspace_id,
                    project_id=args.project_id,
                    project_path=args.project_path,
                    connection_ref=args.connection_ref,
                    credential_secret_ref=args.credential_secret_ref,
                    webhook_signing_secret_ref=args.webhook_signing_secret_ref,
                    actor=_ACTOR,
                    dependencies=dependencies,
                )
            payload = _registered_output(registered)
        elif args.command == "list":
            with engine.connect() as db:
                repositories = list_authorized_repositories(
                    db,
                    workspace_id=args.workspace_id,
                    dependencies=dependencies,
                )
            payload = {
                "items": [
                    {
                        "repositoryId": repository.repository_id,
                        "provider": repository.provider,
                        "projectPath": repository.project_path,
                        "defaultBranch": repository.default_branch,
                    }
                    for repository in repositories
                ]
            }
        else:
            with engine.begin() as db:
                removed = remove_workspace_repository(
                    dependencies.repository_factory(db),
                    repository_id=args.repository_id,
                    expected_revision=args.expected_revision,
                    actor=_ACTOR,
                    dependencies=dependencies,
                )
            payload = {
                "repositoryId": removed.repository_id,
                "status": removed.status.value,
                "revision": removed.revision,
            }
    except SourceControlError:
        _write(errors, {"status": "DENIED", "reasonCode": "REPOSITORY_CONFLICT"})
        return 3
    except (SQLAlchemyError, OSError, ValueError):
        _write(errors, {"status": "FAILED"})
        return 1
    _write(output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
