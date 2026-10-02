import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.model_gateway.application import CatalogDependencies
from control_plane.app.modules.model_gateway.application.checks import currentness
from control_plane.app.modules.model_gateway.domain import CatalogError, Deployment, DeploymentState
from control_plane.app.modules.model_gateway.domain.checks import CheckState, InputCurrentness
from control_plane.app.modules.model_gateway.domain.connections import CheckKind, digest
from control_plane.app.modules.model_gateway.domain.dossiers import (
    CheckEvidenceReference,
    CreateValidationDossier,
    DeclaredMaterial,
    DossierCheckCoverage,
    MaterialCategory,
    MaterialCoverage,
    MaterialExpiration,
    ValidationDossier,
    ValidationDossierProjection,
)
from control_plane.app.modules.model_gateway.ports.checks import ConnectionDirectoryPort
from control_plane.app.modules.model_gateway.ports.dossiers import ValidationDossierRepository
from control_plane.app.shared.api.request_id import current_request_id


class ModelValidationDossiers:
    def __init__(
        self,
        repository: ValidationDossierRepository,
        dependencies: CatalogDependencies,
        directory: ConnectionDirectoryPort,
    ) -> None:
        self.repository, self.dependencies, self.directory = repository, dependencies, directory

    def _deployment(self, deployment_id: str, *, lock: bool = False) -> Deployment:
        value = self.repository.get(deployment_id, for_update=lock)
        if value is None:
            raise CatalogError("MODEL_DEPLOYMENT_NOT_FOUND")
        return value

    def create(
        self,
        deployment_id: str,
        request: CreateValidationDossier,
        *,
        expected_revision: int,
        actor: str,
    ) -> ValidationDossier:
        candidate = self._deployment(deployment_id, lock=True)
        if candidate.revision != expected_revision:
            raise CatalogError("MODEL_DEPLOYMENT_REVISION_CONFLICT")
        if candidate.state == DeploymentState.ARCHIVED:
            raise CatalogError("MODEL_DEPLOYMENT_ARCHIVED")
        references = []
        kinds = set()
        for check_id in sorted(request.check_ids):
            check = self.repository.check(check_id, for_update=True)
            if check is None or check.deployment_id != deployment_id:
                raise CatalogError("MODEL_DOSSIER_CHECK_NOT_FOUND")
            if (
                check.input.deployment_id != deployment_id
                or check.input.deployment_revision != expected_revision
            ):
                raise CatalogError("MODEL_DOSSIER_CHECK_INPUT_CHANGED")
            if check.state in (CheckState.QUEUED, CheckState.RUNNING):
                raise CatalogError("MODEL_DOSSIER_CHECK_NOT_TERMINAL")
            if check.check_kind in kinds:
                raise CatalogError("MODEL_DOSSIER_DUPLICATE_CHECK_KIND")
            kinds.add(check.check_kind)
            references.append(CheckEvidenceReference.capture(check))
        payload = {
            "id": str(self.dependencies.new_id()),
            "deployment_id": deployment_id,
            "candidate_revision": candidate.revision,
            "created_by": actor,
            "created_at": self.dependencies.now()
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "materials": [
                DeclaredMaterial.model_validate(value.model_dump()).model_dump(mode="json")
                for value in request.materials
            ],
            "checks": [value.model_dump(mode="json") for value in references],
        }
        value = ValidationDossier.model_validate(payload | {"snapshot_hash": digest(payload)})
        self.repository.insert_dossier(value)
        record_in_transaction(
            self.repository.db,
            AuditEnvelope(
                id=str(self.dependencies.new_id()),
                occurred_at=self.dependencies.now(),
                actor=actor,
                actor_type="HUMAN",
                action="model_validation_dossier.created",
                target_type="MODEL_VALIDATION_DOSSIER",
                target_id=value.id,
                result="SUCCESS",
                reason=json.dumps(
                    {
                        "deploymentId": deployment_id,
                        "candidateRevision": candidate.revision,
                        "snapshotHash": value.snapshot_hash,
                    }
                ),
                correlation_id=current_request_id() or value.id,
            ),
            self.dependencies.audit,
        )
        return value

    def get(self, deployment_id: str, dossier_id: str) -> ValidationDossier:
        self._deployment(deployment_id)
        value = self.repository.dossier(dossier_id)
        if value is None or value.deployment_id != deployment_id:
            raise CatalogError("MODEL_VALIDATION_DOSSIER_NOT_FOUND")
        return value

    def list(
        self, deployment_id: str, *, cursor: str | None, page_size: int
    ) -> tuple[list[ValidationDossier], str | None]:
        self._deployment(deployment_id)
        before_at = None
        before_id = None
        if cursor is not None:
            try:
                values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (
                    not isinstance(values, list)
                    or len(values) != 3
                    or not all(isinstance(v, str) for v in values)
                    or values[0] != deployment_id
                ):
                    raise ValueError
                before_at = datetime.fromisoformat(values[1])
                if before_at.tzinfo is None:
                    raise ValueError
                before_id = str(UUID(values[2]))
            except (ValueError, TypeError, binascii.Error):
                raise CatalogError("INVALID_MODEL_DOSSIER_CURSOR") from None
        rows = self.repository.list_dossiers(
            deployment_id, before_at=before_at, before_id=before_id, limit=page_size + 1
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([deployment_id, last.created_at.isoformat(), last.id]).encode()
            ).decode()
        return items, next_cursor

    def project(self, dossier: ValidationDossier, *, now: datetime) -> ValidationDossierProjection:
        candidate = self._deployment(dossier.deployment_id)
        reasons: list[
            Literal["CANDIDATE_CHANGED", "CANDIDATE_ARCHIVED", "CHECK_STALE", "CHECK_UNVERIFIABLE"]
        ] = []
        if candidate.revision != dossier.candidate_revision:
            reasons.append("CANDIDATE_CHANGED")
        if candidate.state == DeploymentState.ARCHIVED:
            reasons.append("CANDIDATE_ARCHIVED")
        bound = {reference.check_kind: reference for reference in dossier.checks}
        checks = []
        for kind in CheckKind:
            reference = bound.get(kind)
            if reference is None:
                checks.append(
                    DossierCheckCoverage(
                        check_kind=kind,
                        status="NOT_PROVIDED",
                        check_id=None,
                        currentness=None,
                        currentness_reasons=(),
                    )
                )
                continue
            check = self.repository.check(reference.check_id)
            if check is None:
                state, check_reasons = InputCurrentness.UNVERIFIABLE, ["CHECK_UNAVAILABLE"]
            elif (
                check.deployment_id != dossier.deployment_id
                or check.input.deployment_revision != dossier.candidate_revision
                or check.state in (CheckState.QUEUED, CheckState.RUNNING)
                or CheckEvidenceReference.capture(check) != reference
            ):
                state, check_reasons = InputCurrentness.UNVERIFIABLE, ["CHECK_SNAPSHOT_MISMATCH"]
            else:
                state, original_reasons = currentness(check, candidate, self.directory)
                check_reasons = [value.value for value in original_reasons]
            checks.append(
                DossierCheckCoverage.model_validate(
                    dict(
                        check_kind=kind,
                        status="PROVIDED",
                        check_id=reference.check_id,
                        currentness=state,
                        currentness_reasons=check_reasons,
                    )
                )
            )
            if state == InputCurrentness.STALE and "CHECK_STALE" not in reasons:
                reasons.append("CHECK_STALE")
            if state == InputCurrentness.UNVERIFIABLE and "CHECK_UNVERIFIABLE" not in reasons:
                reasons.append("CHECK_UNVERIFIABLE")
        expirations = [
            MaterialExpiration(
                material_index=index,
                expiration="NOT_DECLARED"
                if item.expires_at is None
                else "EXPIRED"
                if item.expires_at <= now
                else "NOT_EXPIRED",
            )
            for index, item in enumerate(dossier.materials)
        ]
        coverage = []
        for category in MaterialCategory:
            indices = [
                index for index, item in enumerate(dossier.materials) if item.category == category
            ]
            expired = sum(expirations[index].expiration == "EXPIRED" for index in indices)
            coverage.append(
                MaterialCoverage(
                    category=category,
                    registered_count=len(indices),
                    expired_count=expired,
                    status="MISSING"
                    if not indices
                    else "EXPIRED"
                    if expired == len(indices)
                    else "DECLARED",
                )
            )
        state = InputCurrentness.CURRENT
        if any(reason != "CHECK_UNVERIFIABLE" for reason in reasons):
            state = InputCurrentness.STALE
        elif reasons:
            state = InputCurrentness.UNVERIFIABLE
        return ValidationDossierProjection(
            snapshot=dossier,
            currentness=state,
            currentness_reasons=tuple(reasons),
            material_statuses=tuple(expirations),
            material_coverage=tuple(coverage),
            check_coverage=tuple(checks),
        )

    @staticmethod
    def etag(projection: ValidationDossierProjection) -> str:
        return f'"dossier-{digest(projection.model_dump(mode="json"))}"'
