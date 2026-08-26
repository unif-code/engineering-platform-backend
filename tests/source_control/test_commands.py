from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine

from control_plane.app.modules.source_control import (
    RepositoryAuthorizationState,
    RepositoryRemoved,
    RepositoryWorkspaceConflict,
    SourceControlDependencies,
    register_workspace_repository,
    remove_workspace_repository,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlRepository,
)

NOW = datetime(2026, 8, 26, 4, 0, tzinfo=UTC)
REPOSITORY_ID = "gitlab-project-501"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000501"


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedRandom:
    def __init__(self) -> None:
        self._next = 500

    def uuid4(self) -> UUID:
        self._next += 1
        return UUID(f"90000000-0000-0000-0000-{self._next:012d}")


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append_in_transaction(self, _db: object, envelope: object) -> None:
        self.events.append(envelope)


def test_register_repository_stores_only_secret_references(
    isolated_source_control_rw_engine: Engine,
) -> None:
    audit = FakeAudit()
    dependencies = SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=isolated_source_control_rw_engine,
        requirement=None,
        eligibility=None,
        audit=audit,
        clock=FixedClock(),
        random=FixedRandom(),
    )
    with isolated_source_control_rw_engine.begin() as db:
        registered = register_workspace_repository(
            SqlAlchemySourceControlRepository(db),
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="openbao:source-control/gitlab-dev/token",
            webhook_signing_secret_ref="openbao:source-control/gitlab-dev/webhook",
            actor="SYSTEM",
            dependencies=dependencies,
        )

    assert registered.status is RepositoryAuthorizationState.AUTHORIZED
    assert registered.credential_secret_ref == "openbao:source-control/gitlab-dev/token"
    assert "glpat-" not in registered.model_dump_json().lower()
    assert len(audit.events) == 1


def test_repository_cannot_move_workspace_and_removal_is_terminal(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies = SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=isolated_source_control_rw_engine,
        requirement=None,
        eligibility=None,
        audit=FakeAudit(),
        clock=FixedClock(),
        random=FixedRandom(),
    )
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        register_workspace_repository(
            repository,
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref=None,
            actor="SYSTEM",
            dependencies=dependencies,
        )
        with pytest.raises(RepositoryWorkspaceConflict):
            register_workspace_repository(
                repository,
                repository_id=REPOSITORY_ID,
                workspace_id="20000000-0000-0000-0000-000000000599",
                project_id="101",
                project_path="platform/backend",
                connection_ref="gitlab-dev",
                credential_secret_ref="secret-ref:credential",
                webhook_signing_secret_ref=None,
                actor="SYSTEM",
                dependencies=dependencies,
            )
        removed = remove_workspace_repository(
            repository,
            repository_id=REPOSITORY_ID,
            expected_revision=1,
            actor="SYSTEM",
            dependencies=dependencies,
        )
        with pytest.raises(RepositoryRemoved):
            remove_workspace_repository(
                repository,
                repository_id=REPOSITORY_ID,
                expected_revision=2,
                actor="SYSTEM",
                dependencies=dependencies,
            )

    assert removed.status is RepositoryAuthorizationState.REMOVED
    assert removed.revision == 2
