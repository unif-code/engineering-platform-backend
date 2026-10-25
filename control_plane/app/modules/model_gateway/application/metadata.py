import base64
import json
from datetime import datetime
from uuid import UUID

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.model_gateway.application import CatalogDependencies
from control_plane.app.modules.model_gateway.domain import CatalogError, Deployment, DeploymentState
from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked
from control_plane.app.modules.model_gateway.domain.metadata import (
    MetadataAdmission,
    MetadataApplicability,
    MetadataBlocked,
    MetadataInput,
    MetadataObservation,
    MetadataReason,
    MetadataSourceDefinition,
    MetadataState,
)
from control_plane.app.modules.model_gateway.ports.checks import ConnectionDirectoryPort
from control_plane.app.modules.model_gateway.ports.metadata import (
    MetadataRepository,
    MetadataSourceDirectoryPort,
)
from control_plane.app.shared.api.request_id import current_request_id


def resolve_metadata_input(
    candidate: Deployment,
    connections: ConnectionDirectoryPort,
    sources: MetadataSourceDirectoryPort,
) -> tuple[MetadataInput, MetadataSourceDefinition]:
    if candidate.state is not DeploymentState.DRAFT:
        raise MetadataBlocked(MetadataReason.CANDIDATE_ARCHIVED)
    environment, source = sources.resolve(candidate.connection_ref)
    try:
        connection_environment, connection = connections.resolve(candidate.connection_ref)
    except CheckBlocked:
        raise MetadataBlocked(MetadataReason.CONNECTION_UNAVAILABLE) from None
    if (
        environment != connection_environment
        or source.connection_ref != connection.reference
        or candidate.provider_kind != source.provider_kind
        or connection.provider_kind != source.provider_kind
        or connection.region != source.region
        or (source.workspace_id is not None and source.workspace_id != connection.workspace_id)
    ):
        raise MetadataBlocked(MetadataReason.SOURCE_SCOPE_MISMATCH)
    if candidate.provider_model_id not in source.allowed_model_ids:
        raise MetadataBlocked(MetadataReason.SOURCE_NOT_APPROVED)
    return MetadataInput.capture(candidate, connection, source, environment), source


def metadata_admission(
    candidate: Deployment,
    connections: ConnectionDirectoryPort,
    sources: MetadataSourceDirectoryPort,
) -> MetadataAdmission:
    try:
        current, _ = resolve_metadata_input(candidate, connections, sources)
        return MetadataAdmission(available=True, reason=None, source=current.source)
    except MetadataBlocked as error:
        return MetadataAdmission(available=False, reason=error.reason, source=None)


def metadata_applicability(
    value: MetadataObservation,
    candidate: Deployment,
    connections: ConnectionDirectoryPort,
    sources: MetadataSourceDirectoryPort,
) -> tuple[MetadataApplicability, list[str]]:
    if (
        candidate.state is not DeploymentState.DRAFT
        or candidate.revision != value.input.deployment_revision
    ):
        return MetadataApplicability.STALE, ["CANDIDATE_CHANGED"]
    try:
        current, _ = resolve_metadata_input(candidate, connections, sources)
    except MetadataBlocked as error:
        state = (
            MetadataApplicability.STALE
            if error.reason
            in {
                MetadataReason.SOURCE_NOT_APPROVED,
                MetadataReason.SOURCE_SCOPE_MISMATCH,
                MetadataReason.CANDIDATE_ARCHIVED,
            }
            else MetadataApplicability.UNVERIFIABLE
        )
        return state, [error.reason.value]
    if current != value.input:
        return MetadataApplicability.STALE, ["INPUT_BINDING_CHANGED"]
    if value.material_currentness is MetadataApplicability.STALE:
        return MetadataApplicability.STALE, ["MATERIAL_CHANGED_DURING_OBSERVATION"]
    if (
        value.state not in {MetadataState.QUEUED, MetadataState.RUNNING}
        and value.material_currentness is MetadataApplicability.UNVERIFIABLE
    ):
        return MetadataApplicability.UNVERIFIABLE, ["MATERIAL_UNVERIFIABLE_AT_COMPLETION"]
    # Only public configuration is compared; no private Secret or Provider is read here.
    return MetadataApplicability.MATCHING, []


def metadata_audit(
    repository: MetadataRepository,
    value: MetadataObservation,
    dependencies: CatalogDependencies,
    *,
    terminal: bool,
) -> None:
    record_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.new_id()),
            occurred_at=dependencies.now(),
            actor=value.requested_by,
            actor_type="HUMAN",
            action="model_metadata_observation.completed"
            if terminal
            else "model_metadata_observation.requested",
            target_type="MODEL_METADATA_OBSERVATION",
            target_id=value.id,
            result="SUCCESS"
            if value.state
            in {MetadataState.QUEUED, MetadataState.REPORTED, MetadataState.NOT_FOUND}
            else "REJECTED",
            reason=json.dumps(
                {
                    "deploymentId": value.deployment_id,
                    "inputDigest": value.input.input_digest,
                    "state": value.state,
                    "reason": value.reason,
                    "responseSha256": value.response_sha256,
                }
            ),
            correlation_id=current_request_id() or value.id,
        ),
        dependencies.audit,
    )


class ModelMetadataObservations:
    def __init__(
        self,
        repository: MetadataRepository,
        dependencies: CatalogDependencies,
        connections: ConnectionDirectoryPort,
        sources: MetadataSourceDirectoryPort,
    ) -> None:
        self.repository, self.dependencies = repository, dependencies
        self.connections, self.sources = connections, sources

    def deployment(self, deployment_id: str, *, for_update: bool = False) -> Deployment:
        value = self.repository.get(deployment_id, for_update=for_update)
        if value is None:
            raise CatalogError("MODEL_DEPLOYMENT_NOT_FOUND")
        return value

    def accept(
        self, deployment_id: str, *, expected_revision: int, source_fingerprint: str, actor: str
    ) -> MetadataObservation:
        candidate = self.deployment(deployment_id, for_update=True)
        if candidate.revision != expected_revision:
            raise CatalogError("MODEL_DEPLOYMENT_REVISION_CONFLICT")
        try:
            current, _ = resolve_metadata_input(candidate, self.connections, self.sources)
        except MetadataBlocked as error:
            raise CatalogError(f"MODEL_METADATA_{error.reason.value}") from None
        if current.source.fingerprint != source_fingerprint:
            raise CatalogError("MODEL_METADATA_SOURCE_CHANGED")
        if self.repository.active_observation(deployment_id):
            raise CatalogError("MODEL_METADATA_OBSERVATION_ACTIVE")
        value = MetadataObservation(
            id=str(self.dependencies.new_id()),
            deployment_id=deployment_id,
            revision=1,
            requested_by=actor,
            requested_at=self.dependencies.now(),
            input=current,
            state=MetadataState.QUEUED,
        )
        if not self.repository.insert_observation(value):
            raise CatalogError("MODEL_METADATA_OBSERVATION_ACTIVE")
        metadata_audit(self.repository, value, self.dependencies, terminal=False)
        return value

    def get(self, deployment_id: str, observation_id: str) -> MetadataObservation:
        self.deployment(deployment_id)
        value = self.repository.observation(observation_id)
        if value is None or value.deployment_id != deployment_id:
            raise CatalogError("MODEL_METADATA_OBSERVATION_NOT_FOUND")
        return value

    def list(
        self, deployment_id: str, *, cursor: str | None, page_size: int
    ) -> tuple[list[MetadataObservation], str | None]:
        self.deployment(deployment_id)
        before_at = None
        before_id = None
        if cursor is not None:
            try:
                values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (
                    not isinstance(values, list)
                    or len(values) != 3
                    or not all(isinstance(x, str) for x in values)
                    or values[0] != deployment_id
                ):
                    raise ValueError
                before_at = datetime.fromisoformat(values[1])
                if before_at.tzinfo is None:
                    raise ValueError
                before_id = str(UUID(values[2]))
            except (ValueError, TypeError):
                raise CatalogError("INVALID_MODEL_METADATA_CURSOR") from None
        rows = self.repository.list_observations(
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
