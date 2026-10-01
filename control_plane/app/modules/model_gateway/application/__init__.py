import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from pydantic import TypeAdapter

from control_plane.app.modules.audit import (
    AuditEnvelope,
    TransactionalAuditAppender,
    record_in_transaction,
)
from control_plane.app.modules.model_gateway.domain import (
    ArchiveReason,
    CatalogError,
    CreateDeployment,
    Deployment,
    DeploymentKey,
    DeploymentState,
    PatchDeployment,
)
from control_plane.app.modules.model_gateway.ports import DeploymentRepository
from control_plane.app.shared.api.request_id import current_request_id


@dataclass(frozen=True)
class CatalogDependencies:
    audit: TransactionalAuditAppender
    now: Callable[[], datetime]
    new_id: Callable[[], object]


class ModelDeploymentCatalog:
    def __init__(self, repository: DeploymentRepository, dependencies: CatalogDependencies) -> None:
        self.repository = repository
        self.dependencies = dependencies

    def get(self, deployment_id: str, *, for_update: bool = False) -> Deployment:
        value = self.repository.get(deployment_id, for_update=for_update)
        if value is None:
            raise CatalogError("MODEL_DEPLOYMENT_NOT_FOUND")
        return value

    def list(
        self,
        *,
        state: DeploymentState | None,
        query: str,
        cursor: str | None,
        page_size: int,
    ) -> tuple[list[Deployment], str | None]:
        after_key = None
        if cursor is not None:
            try:
                values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (
                    not isinstance(values, list)
                    or len(values) != 4
                    or values[:3] != [1, state, query]
                ):
                    raise ValueError
                after_key = TypeAdapter(DeploymentKey).validate_python(values[3])
            except (ValueError, TypeError, binascii.Error):
                raise CatalogError("INVALID_MODEL_DEPLOYMENT_CURSOR") from None
        rows = self.repository.list(
            state=state, query=query, after_key=after_key, limit=page_size + 1
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            payload = json.dumps([1, state, query, items[-1].deployment_key], separators=(",", ":"))
            next_cursor = base64.urlsafe_b64encode(payload.encode()).decode("ascii")
        return items, next_cursor

    def create(self, request: CreateDeployment, *, actor: str) -> Deployment:
        now = self.dependencies.now()
        value = Deployment(
            **request.model_dump(),
            id=str(self.dependencies.new_id()),
            revision=1,
            state=DeploymentState.DRAFT,
            created_by=actor,
            updated_by=actor,
            created_at=now,
            updated_at=now,
        )
        saved = self.repository.insert(value)
        if saved is None:
            raise CatalogError("MODEL_DEPLOYMENT_KEY_CONFLICT")
        self._audit(saved, actor=actor, action="created")
        return saved

    def patch(
        self,
        deployment_id: str,
        request: PatchDeployment,
        *,
        expected_revision: int,
        actor: str,
    ) -> Deployment:
        current = self._writable(deployment_id, expected_revision)
        value = Deployment.model_validate(
            current.model_dump()
            | request.model_dump(exclude_unset=True)
            | {
                "revision": current.revision + 1,
                "updated_by": actor,
                "updated_at": self.dependencies.now(),
            }
        )
        return self._save(value, actor=actor, action="updated", expected_revision=expected_revision)

    def archive(
        self,
        deployment_id: str,
        reason: str,
        *,
        expected_revision: int,
        actor: str,
    ) -> Deployment:
        reason = TypeAdapter(ArchiveReason).validate_python(reason)
        current = self._writable(deployment_id, expected_revision)
        now = self.dependencies.now()
        value = Deployment.model_validate(
            current.model_dump()
            | {
                "state": DeploymentState.ARCHIVED,
                "revision": current.revision + 1,
                "updated_by": actor,
                "updated_at": now,
                "archived_by": actor,
                "archived_at": now,
                "archive_reason": reason,
            }
        )
        return self._save(
            value, actor=actor, action="archived", expected_revision=expected_revision
        )

    def _writable(self, deployment_id: str, expected_revision: int) -> Deployment:
        value = self.get(deployment_id, for_update=True)
        if value.revision != expected_revision:
            raise CatalogError("MODEL_DEPLOYMENT_REVISION_CONFLICT")
        if value.state is DeploymentState.ARCHIVED:
            raise CatalogError("MODEL_DEPLOYMENT_ARCHIVED")
        return value

    def _save(
        self, value: Deployment, *, actor: str, action: str, expected_revision: int
    ) -> Deployment:
        if not self.repository.save(value, expected_revision=expected_revision):
            raise CatalogError("MODEL_DEPLOYMENT_REVISION_CONFLICT")
        self._audit(value, actor=actor, action=action)
        return value

    def _audit(self, value: Deployment, *, actor: str, action: str) -> None:
        record_in_transaction(
            self.repository.db,
            AuditEnvelope(
                id=str(self.dependencies.new_id()),
                occurred_at=self.dependencies.now(),
                actor=actor,
                actor_type="HUMAN",
                action=f"model_deployment.{action}",
                target_type="MODEL_DEPLOYMENT",
                target_id=value.id,
                result="SUCCESS",
                reason=json.dumps({"revision": value.revision, "reason": value.archive_reason}),
                correlation_id=current_request_id() or str(self.dependencies.new_id()),
            ),
            self.dependencies.audit,
        )
