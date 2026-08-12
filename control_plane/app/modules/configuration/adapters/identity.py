from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.configuration.domain import (
    Draft,
    PolicyKey,
    PolicySnapshot,
    PolicySnapshotUnavailable,
    PreviewItem,
    PublishedVersion,
    ValidationIssue,
)
from control_plane.app.modules.identity import (
    OwnedPolicySnapshotUnavailable,
    active_policy_snapshot,
    archive_policy_candidates,
    archive_policy_draft,
    claim_configuration_idempotency,
    complete_configuration_idempotency,
    configuration_idempotency_by_scope,
    create_policy_draft,
    effective_identity_policy,
    list_policy_versions,
    locked_active_policy_snapshot,
    policy_catalog,
    policy_draft,
    policy_version_snapshot,
    preview_policy_candidate,
    publish_policy_version,
    save_policy_draft_preview,
    save_policy_draft_validation,
    update_policy_draft,
    validate_policy_candidate,
)


class IdentityPolicyOwner:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def claim_idempotency(self, **values: Any) -> bool:
        return claim_configuration_idempotency(self.db, **values)

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any:
        return configuration_idempotency_by_scope(
            self.db,
            actor,
            operation,
            idempotency_key,
            for_update=for_update,
        )

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool:
        return complete_configuration_idempotency(
            self.db,
            record_id,
            http_status=http_status,
            result_metadata=result_metadata,
            sealed_response=sealed_response,
            now=now,
        )

    def catalog(self, namespace: str) -> list[PolicyKey]:
        try:
            owned = policy_catalog(self.db, namespace)
        except OwnedPolicySnapshotUnavailable as exc:
            raise PolicySnapshotUnavailable(namespace) from exc
        return [PolicyKey.model_validate(item.model_dump()) for item in owned]

    def active_snapshot(self, namespace: str) -> PolicySnapshot:
        try:
            owned = active_policy_snapshot(self.db, namespace)
        except OwnedPolicySnapshotUnavailable as exc:
            raise PolicySnapshotUnavailable(namespace) from exc
        return PolicySnapshot.model_validate(owned.model_dump())

    def locked_active_snapshot(self, namespace: str) -> PolicySnapshot:
        try:
            owned = locked_active_policy_snapshot(self.db, namespace)
        except OwnedPolicySnapshotUnavailable as exc:
            raise PolicySnapshotUnavailable(namespace) from exc
        return PolicySnapshot.model_validate(owned.model_dump())

    def version_snapshot(
        self,
        namespace: str,
        scope: str,
        version: int,
    ) -> PolicySnapshot | None:
        try:
            owned = policy_version_snapshot(self.db, namespace, scope, version)
        except OwnedPolicySnapshotUnavailable as exc:
            raise PolicySnapshotUnavailable(namespace) from exc
        return None if owned is None else PolicySnapshot.model_validate(owned.model_dump())

    def list_versions(
        self,
        namespace: str,
        scope: str,
        *,
        before_version: int | None,
        limit: int,
    ) -> list[PublishedVersion]:
        try:
            owned = list_policy_versions(
                self.db,
                namespace,
                scope,
                before_version=before_version,
                limit=limit,
            )
        except OwnedPolicySnapshotUnavailable as exc:
            raise PolicySnapshotUnavailable(namespace) from exc
        return [PublishedVersion.model_validate(item.model_dump()) for item in owned]

    def validate_candidate(
        self,
        namespace: str,
        *,
        schema_revision: int,
        values: dict[str, Any],
    ) -> list[ValidationIssue]:
        return [
            ValidationIssue.model_validate(issue.model_dump())
            for issue in validate_policy_candidate(
                self.db,
                namespace,
                schema_revision=schema_revision,
                values=values,
            )
        ]

    @staticmethod
    def _draft(owned: Any) -> Draft:
        return Draft.model_validate(owned.model_dump())

    def create_draft(self, **values: Any) -> Draft:
        return self._draft(create_policy_draft(self.db, **values))

    def draft(self, draft_id: str, *, for_update: bool = False) -> Draft | None:
        owned = policy_draft(self.db, draft_id, for_update=for_update)
        return None if owned is None else self._draft(owned)

    def update_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        content: dict[str, Any],
        content_hash: str,
        stale: bool,
        now: datetime,
    ) -> Draft | None:
        owned = update_policy_draft(
            self.db,
            draft_id,
            expected_revision=expected_revision,
            content=content,
            content_hash=content_hash,
            stale=stale,
            now=now,
        )
        return None if owned is None else self._draft(owned)

    def save_validation(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
        now: datetime,
    ) -> Draft | None:
        owned = save_policy_draft_validation(
            self.db,
            draft_id,
            expected_revision=expected_revision,
            evidence=evidence,
            dependency_versions=dependency_versions,
            now=now,
        )
        return None if owned is None else self._draft(owned)

    def preview_candidate(
        self,
        namespace: str,
        *,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> list[PreviewItem]:
        try:
            owned = preview_policy_candidate(
                self.db,
                namespace,
                before=before,
                after=after,
            )
        except OwnedPolicySnapshotUnavailable as exc:
            raise PolicySnapshotUnavailable(namespace) from exc
        return [PreviewItem.model_validate(item.model_dump()) for item in owned]

    def save_preview(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
    ) -> Draft | None:
        owned = save_policy_draft_preview(
            self.db,
            draft_id,
            expected_revision=expected_revision,
            evidence=evidence,
            dependency_versions=dependency_versions,
        )
        return None if owned is None else self._draft(owned)

    def publish_version(self, **values: Any) -> PublishedVersion | None:
        owned = publish_policy_version(self.db, **values)
        return None if owned is None else PublishedVersion.model_validate(owned.model_dump())

    def draft_archive_after(self, namespace: str) -> timedelta:
        try:
            return effective_identity_policy(self.db, namespace).draft_archive_after
        except OwnedPolicySnapshotUnavailable as exc:
            raise PolicySnapshotUnavailable(namespace) from exc

    def archive_candidates(
        self,
        namespace: str,
        scope: str,
        *,
        cutoff: datetime,
        limit: int,
    ) -> list[Draft]:
        owned = archive_policy_candidates(
            self.db,
            namespace,
            scope,
            cutoff=cutoff,
            limit=limit,
        )
        return [self._draft(item) for item in owned]

    def archive_draft(self, **values: Any) -> bool:
        return archive_policy_draft(self.db, **values)
