import base64
import json
from datetime import datetime
from uuid import UUID

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.model_gateway.application import (
    CatalogDependencies,
)
from control_plane.app.modules.model_gateway.domain import CatalogError, Deployment, DeploymentState
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckBlocked,
    CheckInputSnapshot,
    CheckReason,
    CheckState,
    ConnectionCheck,
    CurrentnessReason,
    InputCurrentness,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    ADAPTER_VERSION,
    PROBE_VERSION,
)
from control_plane.app.modules.model_gateway.ports.checks import (
    CheckRepository,
    ConnectionDirectoryPort,
)
from control_plane.app.shared.api.request_id import current_request_id


def check_audit(
    repository: CheckRepository,
    check: ConnectionCheck,
    dependencies: CatalogDependencies,
    *,
    terminal: bool,
    input_currentness: InputCurrentness | None = None,
) -> None:
    record_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.new_id()),
            occurred_at=dependencies.now(),
            actor=check.requested_by,
            actor_type="HUMAN",
            action="model_connection_check.completed"
            if terminal
            else "model_connection_check.accepted",
            target_type="MODEL_CONNECTION_CHECK",
            target_id=check.id,
            result=check.state.value,
            reason=json.dumps(
                {
                    "deploymentId": check.deployment_id,
                    "candidateRevision": check.input.deployment_revision,
                    "checkRevision": check.revision,
                    "reason": check.reason,
                    "inputCurrentness": input_currentness,
                }
            ),
            correlation_id=current_request_id() or check.id,
        ),
        dependencies.audit,
    )


def currentness(
    check: ConnectionCheck, deployment: Deployment, directory: ConnectionDirectoryPort
) -> tuple[InputCurrentness, list[CurrentnessReason]]:
    reasons: list[CurrentnessReason] = []
    if deployment.revision != check.input.deployment_revision:
        reasons.append(CurrentnessReason.CANDIDATE_CHANGED)
    if deployment.state is DeploymentState.ARCHIVED:
        reasons.append(CurrentnessReason.CANDIDATE_ARCHIVED)
    if check.input.adapter_version != ADAPTER_VERSION or check.input.probe_version != PROBE_VERSION:
        reasons.append(CurrentnessReason.PROBE_CHANGED)
    try:
        environment, connection = directory.resolve(deployment.connection_ref)
        if check.input.material_version != connection.material_version:
            reasons.append(CurrentnessReason.MATERIAL_VERSION_CHANGED)
        if (
            check.input.environment != environment
            or check.input.connection_fingerprint != connection.fingerprint
        ):
            reasons.append(CurrentnessReason.CONNECTION_CHANGED)
    except CheckBlocked:
        reasons.append(CurrentnessReason.CONNECTION_UNAVAILABLE)
    if check.material_currentness is InputCurrentness.STALE:
        if CurrentnessReason.MATERIAL_VERSION_CHANGED not in reasons:
            reasons.append(CurrentnessReason.MATERIAL_VERSION_CHANGED)
    elif check.material_currentness is InputCurrentness.UNVERIFIABLE:
        reasons.append(CurrentnessReason.MATERIAL_UNVERIFIABLE)
    unknown = {CurrentnessReason.CONNECTION_UNAVAILABLE, CurrentnessReason.MATERIAL_UNVERIFIABLE}
    state = InputCurrentness.CURRENT
    if any(reason not in unknown for reason in reasons):
        state = InputCurrentness.STALE
    elif reasons:
        state = InputCurrentness.UNVERIFIABLE
    return state, reasons


class ModelConnectionChecks:
    def __init__(
        self,
        repository: CheckRepository,
        dependencies: CatalogDependencies,
        directory: ConnectionDirectoryPort,
    ) -> None:
        self.repository, self.dependencies, self.directory = repository, dependencies, directory

    def deployment(self, deployment_id: str, *, for_update: bool = False) -> Deployment:
        value = self.repository.get(deployment_id, for_update=for_update)
        if value is None:
            raise CatalogError("MODEL_DEPLOYMENT_NOT_FOUND")
        return value

    def get(self, deployment_id: str, check_id: str) -> ConnectionCheck:
        value = self.repository.check(check_id)
        if value is None or value.deployment_id != deployment_id:
            raise CatalogError("MODEL_CONNECTION_CHECK_NOT_FOUND")
        return value

    def accept(self, deployment_id: str, *, expected_revision: int, actor: str) -> ConnectionCheck:
        deployment = self.deployment(deployment_id, for_update=True)
        if deployment.revision != expected_revision:
            raise CatalogError("MODEL_DEPLOYMENT_REVISION_CONFLICT")
        if deployment.state is DeploymentState.ARCHIVED:
            raise CatalogError("MODEL_DEPLOYMENT_ARCHIVED")
        if self.repository.active_check(deployment_id) is not None:
            raise CatalogError("MODEL_CONNECTION_CHECK_ACTIVE")
        connection = None
        environment = None
        reason = None
        try:
            environment, connection = self.directory.resolve(deployment.connection_ref)
            if (
                connection.provider_kind != deployment.provider_kind
                or deployment.provider_model_id not in connection.allowed_model_ids
            ):
                reason = CheckReason.MODEL_NOT_ALLOWED
        except CheckBlocked as error:
            reason = error.reason
        now = self.dependencies.now()
        check = ConnectionCheck(
            id=str(self.dependencies.new_id()),
            deployment_id=deployment_id,
            revision=1,
            requested_by=actor,
            requested_at=now,
            input=CheckInputSnapshot.capture(deployment, connection, environment),
            state=CheckState.BLOCKED if reason else CheckState.QUEUED,
            reason=reason,
            attempt=0,
            execution_token=None,
            started_at=None,
            deadline_at=None,
            finished_at=now if reason else None,
            elapsed_ms=None,
            provider_request_id=None,
            reported_model_id=None,
            usage=None,
            material_currentness=InputCurrentness.UNVERIFIABLE,
        )
        if not self.repository.insert_check(check):
            raise CatalogError("MODEL_CONNECTION_CHECK_ACTIVE")
        check_audit(self.repository, check, self.dependencies, terminal=False)
        if reason is not None:
            check_audit(self.repository, check, self.dependencies, terminal=True)
        return check

    def list(
        self, deployment_id: str, *, cursor: str | None, page_size: int
    ) -> tuple[list[ConnectionCheck], str | None]:
        self.deployment(deployment_id)
        before_at = None
        before_id = None
        if cursor is not None:
            try:
                values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(values, list) or len(values) != 3 or values[0] != deployment_id:
                    raise ValueError
                before_at = datetime.fromisoformat(values[1])
                if before_at.tzinfo is None:
                    raise ValueError
                before_id = str(UUID(values[2]))
            except (ValueError, TypeError):
                raise CatalogError("INVALID_MODEL_CONNECTION_CHECK_CURSOR") from None
        rows = self.repository.list_checks(
            deployment_id, before_at=before_at, before_id=before_id, limit=page_size + 1
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([deployment_id, last.requested_at.isoformat(), last.id]).encode()
            ).decode()
        return items, next_cursor
