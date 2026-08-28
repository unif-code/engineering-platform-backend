import json
from datetime import UTC, datetime
from io import StringIO

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.identity.adapters.runtime import SystemRandom
from control_plane.app.modules.source_control import SourceControlDependencies
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlRepository,
)
from control_plane.tools.source_control_repository import main
from tests.source_control.conftest import IsolatedSourceControlDatabase

WORKSPACE_ID = "20000000-0000-0000-0000-000000000701"
OTHER_WORKSPACE_ID = "20000000-0000-0000-0000-000000000702"
REPOSITORY_ID = "10000000-0000-0000-0000-000000000701"


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def _dependencies(engine: Engine) -> SourceControlDependencies:
    return SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=engine,
        requirement=None,
        eligibility=None,
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=FixedClock(),
        random=SystemRandom(),
    )


def _register_args(**overrides: str) -> list[str]:
    values = {
        "repository_id": REPOSITORY_ID,
        "workspace_id": WORKSPACE_ID,
        "project_id": "701",
        "project_path": "platform/backend",
        "connection_ref": "gitlab-dev",
        "credential_secret_ref": "secret-ref:gitlab-pat",
        "webhook_signing_secret_ref": "secret-ref:gitlab-webhook",
    }
    values.update(overrides)
    return [
        "register",
        "--repository-id",
        values["repository_id"],
        "--workspace-id",
        values["workspace_id"],
        "--project-id",
        values["project_id"],
        "--project-path",
        values["project_path"],
        "--connection-ref",
        values["connection_ref"],
        "--credential-secret-ref",
        values["credential_secret_ref"],
        "--webhook-signing-secret-ref",
        values["webhook_signing_secret_ref"],
    ]


def _run(
    engine: Engine,
    args: list[str],
) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    exit_code = main(
        args,
        engine=engine,
        dependencies=_dependencies(engine),
        stdout=stdout,
        stderr=stderr,
    )
    return exit_code, stdout.getvalue(), stderr.getvalue()


def test_repository_tool_register_is_idempotent_for_identical_metadata(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    first = _run(isolated_source_control_database.runtime, _register_args())
    replay = _run(isolated_source_control_database.runtime, _register_args())

    assert first[0] == replay[0] == 0
    assert json.loads(first[1]) == json.loads(replay[1])
    with isolated_source_control_database.runtime.connect() as db:
        repository_count = db.execute(
            text("SELECT count(*) FROM source_control.workspace_repository")
        ).scalar_one()
    with isolated_source_control_database.owner.connect() as db:
        audit_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='source_control.repository.registered'"
            )
        ).scalar_one()
    assert (repository_count, audit_count) == (1, 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace_id": OTHER_WORKSPACE_ID},
        {"project_id": "999"},
        {"project_path": "platform/another"},
    ],
)
def test_repository_tool_rejects_conflicting_workspace_or_project_metadata(
    isolated_source_control_rw_engine: Engine,
    overrides: dict[str, str],
) -> None:
    assert _run(isolated_source_control_rw_engine, _register_args())[0] == 0

    exit_code, stdout, stderr = _run(
        isolated_source_control_rw_engine,
        _register_args(**overrides),
    )

    assert exit_code != 0
    assert stdout == ""
    assert json.loads(stderr)["status"] == "DENIED"


def test_repository_tool_list_is_sanitized(
    isolated_source_control_rw_engine: Engine,
) -> None:
    assert _run(isolated_source_control_rw_engine, _register_args())[0] == 0

    exit_code, stdout, stderr = _run(
        isolated_source_control_rw_engine,
        ["list", "--workspace-id", WORKSPACE_ID],
    )

    assert exit_code == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "items": [
            {
                "repositoryId": REPOSITORY_ID,
                "provider": "GITLAB",
                "projectPath": "platform/backend",
                "defaultBranch": "main",
            }
        ]
    }
    serialized = stdout + stderr
    assert all(
        value not in serialized
        for value in (
            "connectionRef",
            "credentialSecretRef",
            "webhookSigningSecretRef",
            "secret-ref:",
            "private-provider-body",
        )
    )


def test_repository_tool_remove_requires_revision_and_preserves_history(
    isolated_source_control_rw_engine: Engine,
) -> None:
    assert _run(isolated_source_control_rw_engine, _register_args())[0] == 0
    with pytest.raises(SystemExit) as missing_revision:
        _run(
            isolated_source_control_rw_engine,
            ["remove", "--repository-id", REPOSITORY_ID],
        )

    exit_code, stdout, stderr = _run(
        isolated_source_control_rw_engine,
        [
            "remove",
            "--repository-id",
            REPOSITORY_ID,
            "--expected-revision",
            "1",
        ],
    )

    assert missing_revision.value.code == 2
    assert exit_code == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "repositoryId": REPOSITORY_ID,
        "status": "REMOVED",
        "revision": 2,
    }
    with isolated_source_control_rw_engine.connect() as db:
        row = db.execute(
            text(
                "SELECT status, revision FROM source_control.workspace_repository "
                "WHERE id=:repository_id"
            ),
            {"repository_id": REPOSITORY_ID},
        ).one()
    assert row == ("REMOVED", 2)
