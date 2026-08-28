import re
from typing import Any

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    InvalidRepositorySecretReference,
    RepositoryAuthorizationState,
    RepositoryNotFound,
    RepositoryRemoved,
    RepositoryWorkspaceConflict,
    StaleRepositoryRevision,
    WorkspaceRepositoryDto,
)
from control_plane.app.modules.source_control.ports import SourceControlRepository

_SECRET_REFERENCE = re.compile(r"^(?:openbao|secret-ref):[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


def _repository_dto(row: Any) -> WorkspaceRepositoryDto:
    return WorkspaceRepositoryDto(
        repository_id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        provider=row["provider"],
        project_id=row["project_id"],
        project_path=row["project_path"],
        default_branch=row["default_branch"],
        connection_ref=row["connection_ref"],
        credential_secret_ref=row["credential_secret_ref"],
        webhook_signing_secret_ref=row["webhook_signing_secret_ref"],
        status=RepositoryAuthorizationState(row["status"]),
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_secret_reference(value: str) -> None:
    if _SECRET_REFERENCE.fullmatch(value) is None:
        raise InvalidRepositorySecretReference("A secret reference is required")


def _audit_repository_change(
    repository: SourceControlRepository,
    *,
    actor: object,
    action: str,
    repository_id: str,
    dependencies: SourceControlDependencies,
) -> None:
    dependencies.audit.append_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=str(actor),
            actor_type="SYSTEM" if str(actor).startswith("SYSTEM") else "HUMAN",
            action=action,
            target_type="workspace_repository",
            target_id=repository_id,
            result="SUCCESS",
            correlation_id=f"source-control:{repository_id}",
        ),
    )


def register_workspace_repository(
    repository: SourceControlRepository,
    *,
    repository_id: str,
    workspace_id: str,
    project_id: str,
    project_path: str,
    connection_ref: str,
    credential_secret_ref: str,
    webhook_signing_secret_ref: str | None,
    actor: object,
    dependencies: SourceControlDependencies,
) -> WorkspaceRepositoryDto:
    _validate_secret_reference(credential_secret_ref)
    if webhook_signing_secret_ref is not None:
        _validate_secret_reference(webhook_signing_secret_ref)
    existing = repository.workspace_repository(repository_id, for_update=True)
    if existing is not None:
        expected = {
            "workspace_id": workspace_id,
            "provider": "GITLAB",
            "project_id": project_id,
            "project_path": project_path,
            "default_branch": "main",
            "connection_ref": connection_ref,
            "credential_secret_ref": credential_secret_ref,
            "webhook_signing_secret_ref": webhook_signing_secret_ref,
        }
        actual = {
            key: str(existing[key]) if key == "workspace_id" else existing[key] for key in expected
        }
        if actual != expected:
            raise RepositoryWorkspaceConflict(repository_id)
        if existing["status"] == RepositoryAuthorizationState.REMOVED.value:
            raise RepositoryRemoved(repository_id)
        return _repository_dto(existing)
    now = dependencies.clock.now()
    inserted = repository.insert_workspace_repository(
        id=repository_id,
        workspace_id=workspace_id,
        provider="GITLAB",
        project_id=project_id,
        project_path=project_path,
        default_branch="main",
        connection_ref=connection_ref,
        credential_secret_ref=credential_secret_ref,
        webhook_signing_secret_ref=webhook_signing_secret_ref,
        status=RepositoryAuthorizationState.AUTHORIZED.value,
        revision=1,
        now=now,
    )
    _audit_repository_change(
        repository,
        actor=actor,
        action="source_control.repository.registered",
        repository_id=repository_id,
        dependencies=dependencies,
    )
    return _repository_dto(inserted)


def remove_workspace_repository(
    repository: SourceControlRepository,
    *,
    repository_id: str,
    expected_revision: int,
    actor: object,
    dependencies: SourceControlDependencies,
) -> WorkspaceRepositoryDto:
    existing = repository.workspace_repository(repository_id, for_update=True)
    if existing is None:
        raise RepositoryNotFound(repository_id)
    if existing["status"] == RepositoryAuthorizationState.REMOVED.value:
        raise RepositoryRemoved(repository_id)
    removed = repository.remove_workspace_repository(
        repository_id,
        expected_revision=expected_revision,
        now=dependencies.clock.now(),
    )
    if removed is None:
        raise StaleRepositoryRevision(repository_id)
    _audit_repository_change(
        repository,
        actor=actor,
        action="source_control.repository.removed",
        repository_id=repository_id,
        dependencies=dependencies,
    )
    return _repository_dto(removed)
