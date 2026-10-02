import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.model_gateway.application import CatalogDependencies
from control_plane.app.modules.model_gateway.domain import CatalogError, Deployment, DeploymentState
from control_plane.app.modules.model_gateway.domain.checks import InputCurrentness
from control_plane.app.modules.model_gateway.domain.connections import digest
from control_plane.app.modules.model_gateway.domain.dossiers import ValidationDossier
from control_plane.app.modules.model_gateway.domain.source_checks import (
    MaterialSourceCheck,
    MaterialSourceCheckProjection,
    SourceCheckCurrentnessReason,
    SourceCheckReason,
    SourceEvidence,
    compare_declared_source,
)
from control_plane.app.modules.model_gateway.ports.source_checks import (
    MaterialSourceCheckRepository,
    ModelMaterialSourcePort,
)
from control_plane.app.shared.api.request_id import current_request_id


class ModelMaterialSourceChecks:
    def __init__(
        self,
        repository: MaterialSourceCheckRepository,
        dependencies: CatalogDependencies,
        sources: ModelMaterialSourcePort,
    ) -> None:
        self.repository, self.dependencies, self.sources = repository, dependencies, sources

    def _deployment(self, deployment_id: str, *, lock: bool = False) -> Deployment:
        value = self.repository.get(deployment_id, for_update=lock)
        if value is None:
            raise CatalogError("MODEL_DEPLOYMENT_NOT_FOUND")
        return value

    def _dossier(self, deployment_id: str, dossier_id: str) -> ValidationDossier:
        value = self.repository.dossier(dossier_id)
        if value is None or value.deployment_id != deployment_id:
            raise CatalogError("MODEL_VALIDATION_DOSSIER_NOT_FOUND")
        return value

    def create(
        self,
        deployment_id: str,
        dossier_id: str,
        *,
        material_index: int,
        expected_revision: int,
        actor: str,
    ) -> MaterialSourceCheck:
        candidate = self._deployment(deployment_id, lock=True)
        if candidate.revision != expected_revision:
            raise CatalogError("MODEL_DEPLOYMENT_REVISION_CONFLICT")
        if candidate.state == DeploymentState.ARCHIVED:
            raise CatalogError("MODEL_DEPLOYMENT_ARCHIVED")
        dossier = self._dossier(deployment_id, dossier_id)
        if dossier.candidate_revision != candidate.revision:
            raise CatalogError("MODEL_SOURCE_DOSSIER_INPUT_CHANGED")
        if type(material_index) is not int or not 0 <= material_index < len(dossier.materials):
            raise CatalogError("MODEL_SOURCE_MATERIAL_NOT_FOUND")
        material = dossier.materials[material_index]
        observed = None
        if material.declared_content_sha256 is not None:
            observed = self.sources.inspect(material.source_reference, material.external_version)
        result, reason = compare_declared_source(material.declared_content_sha256, observed)
        evidence = SourceEvidence() if observed is None else observed
        payload = {
            "id": str(self.dependencies.new_id()),
            "deployment_id": deployment_id,
            "candidate_revision": candidate.revision,
            "dossier_id": dossier.id,
            "dossier_snapshot_hash": dossier.snapshot_hash,
            "material_index": material_index,
            "source_reference": material.source_reference,
            "external_version": material.external_version,
            "declared_content_sha256": material.declared_content_sha256,
            **evidence.model_dump(mode="json", exclude={"reason"}),
            "result": result,
            "reason": reason,
            "created_by": actor,
            "created_at": self.dependencies.now()
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        value = MaterialSourceCheck.model_validate(payload | {"snapshot_hash": digest(payload)})
        self.repository.insert_source_check(value)
        record_in_transaction(
            self.repository.db,
            AuditEnvelope(
                id=str(self.dependencies.new_id()),
                occurred_at=self.dependencies.now(),
                actor=actor,
                actor_type="HUMAN",
                action="model_material_source_check.created",
                target_type="MODEL_MATERIAL_SOURCE_CHECK",
                target_id=value.id,
                result="SUCCESS",
                reason=json.dumps(
                    {
                        "dossierId": value.dossier_id,
                        "dossierSnapshotHash": value.dossier_snapshot_hash,
                        "materialIndex": value.material_index,
                        "snapshotHash": value.snapshot_hash,
                        "result": value.result,
                        "reason": value.reason,
                    }
                ),
                correlation_id=current_request_id() or value.id,
            ),
            self.dependencies.audit,
        )
        return value

    def get(self, deployment_id: str, dossier_id: str, source_check_id: str) -> MaterialSourceCheck:
        self._deployment(deployment_id)
        self._dossier(deployment_id, dossier_id)
        value = self.repository.source_check(source_check_id)
        if value is None or (value.deployment_id, value.dossier_id) != (deployment_id, dossier_id):
            raise CatalogError("MODEL_MATERIAL_SOURCE_CHECK_NOT_FOUND")
        return value

    def list(
        self, deployment_id: str, dossier_id: str, *, cursor: str | None, page_size: int
    ) -> tuple[list[MaterialSourceCheck], str | None]:
        self._deployment(deployment_id)
        self._dossier(deployment_id, dossier_id)
        before_at = None
        before_id = None
        if cursor is not None:
            try:
                values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (
                    not isinstance(values, list)
                    or len(values) != 4
                    or not all(isinstance(v, str) for v in values)
                    or values[:2] != [deployment_id, dossier_id]
                ):
                    raise ValueError
                before_at = datetime.fromisoformat(values[2])
                if before_at.tzinfo is None:
                    raise ValueError
                before_id = str(UUID(values[3]))
            except (ValueError, TypeError, binascii.Error):
                raise CatalogError("INVALID_MODEL_SOURCE_CHECK_CURSOR") from None
        rows = self.repository.list_source_checks(
            deployment_id, dossier_id, before_at=before_at, before_id=before_id, limit=page_size + 1
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    [deployment_id, dossier_id, last.created_at.isoformat(), last.id]
                ).encode()
            ).decode()
        return items, next_cursor

    def project(
        self, record: MaterialSourceCheck, *, now: datetime | None = None
    ) -> MaterialSourceCheckProjection:
        now = now or self.dependencies.now()
        candidate = self._deployment(record.deployment_id)
        dossier = self._dossier(record.deployment_id, record.dossier_id)
        if (
            dossier.snapshot_hash != record.dossier_snapshot_hash
            or not 0 <= record.material_index < len(dossier.materials)
        ):
            raise CatalogError("MODEL_SOURCE_CHECK_BINDING_UNAVAILABLE")
        material = dossier.materials[record.material_index]
        reasons: list[SourceCheckCurrentnessReason] = []
        changed = False
        if candidate.revision != record.candidate_revision:
            changed = True
            reasons.append(SourceCheckCurrentnessReason.CANDIDATE_CHANGED)
        if candidate.state == DeploymentState.ARCHIVED:
            changed = True
            reasons.append(SourceCheckCurrentnessReason.CANDIDATE_ARCHIVED)
        observed = None
        if record.declared_content_sha256 is not None:
            observed = self.sources.inspect(record.source_reference, record.external_version)
        if observed is not None:
            if (
                record.entry_fingerprint is not None
                and observed.entry_fingerprint is not None
                and record.entry_fingerprint != observed.entry_fingerprint
            ):
                changed = True
                reasons.append(SourceCheckCurrentnessReason.SOURCE_BINDING_CHANGED)
            if (
                record.entry_fingerprint is not None
                and observed.reason == SourceCheckReason.SOURCE_NOT_APPROVED
            ):
                changed = True
                reasons.append(SourceCheckCurrentnessReason.SOURCE_NO_LONGER_APPROVED)
            if (
                record.observed_sha256 is not None
                and observed.observed_sha256 is not None
                and (record.observed_sha256, record.observed_bytes)
                != (observed.observed_sha256, observed.observed_bytes)
            ):
                changed = True
                reasons.append(SourceCheckCurrentnessReason.SOURCE_CONTENT_CHANGED)
        unavailable = False
        if record.observed_sha256 is None:
            unavailable = True
            reasons.append(SourceCheckCurrentnessReason.ORIGINAL_MEASUREMENT_UNAVAILABLE)
        if observed is None or observed.reason is not None:
            unavailable = True
            reasons.append(SourceCheckCurrentnessReason.SOURCE_UNVERIFIABLE)
        state = (
            InputCurrentness.STALE
            if changed
            else InputCurrentness.UNVERIFIABLE
            if unavailable
            else InputCurrentness.CURRENT
        )
        expiration = (
            "NOT_DECLARED"
            if material.expires_at is None
            else "EXPIRED"
            if material.expires_at <= now
            else "NOT_EXPIRED"
        )
        return MaterialSourceCheckProjection.model_validate(
            {
                "snapshot": record,
                "currentness": state,
                "currentness_reasons": reasons,
                "material_expires_at": material.expires_at,
                "material_expiration": expiration,
            }
        )

    @staticmethod
    def etag(projection: MaterialSourceCheckProjection) -> str:
        return f'"source-check-{digest(projection.model_dump(mode="json"))}"'
