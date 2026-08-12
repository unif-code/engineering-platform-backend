import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from control_plane.app.modules.identity.domain.configuration_policy import (
    IDENTITY_POLICY_SCHEMA_REVISION,
    OwnedPolicyDraft,
    OwnedPolicyKey,
    OwnedPolicyPreviewItem,
    OwnedPolicySnapshot,
    OwnedPolicySnapshotUnavailable,
    OwnedPolicyValidationIssue,
    OwnedPublishedPolicyVersion,
    identity_policy_preview,
    validate_and_materialize_identity_policy,
    validate_identity_policy_catalog,
)
from control_plane.app.modules.identity.domain.policy import EffectiveIdentityPolicy
from control_plane.app.modules.identity.ports.configuration_policy import (
    IdentityPolicyOwnerRepository,
)


def policy_catalog(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
) -> list[OwnedPolicyKey]:
    catalog = repository.catalog(namespace)
    if validate_identity_policy_catalog(catalog):
        raise OwnedPolicySnapshotUnavailable(namespace)
    return catalog


def claim_configuration_idempotency(
    repository: IdentityPolicyOwnerRepository,
    **values: Any,
) -> bool:
    return repository.claim_configuration_idempotency(**values)


def configuration_idempotency_by_scope(
    repository: IdentityPolicyOwnerRepository,
    actor: str,
    operation: str,
    idempotency_key: str,
    *,
    for_update: bool = False,
) -> Any:
    return repository.configuration_idempotency_by_scope(
        actor,
        operation,
        idempotency_key,
        for_update=for_update,
    )


def complete_configuration_idempotency(
    repository: IdentityPolicyOwnerRepository,
    record_id: str,
    **values: Any,
) -> bool:
    return repository.complete_configuration_idempotency(record_id, **values)


def create_policy_draft(
    repository: IdentityPolicyOwnerRepository,
    **values: Any,
) -> OwnedPolicyDraft:
    return repository.insert_draft(**values)


def policy_draft(
    repository: IdentityPolicyOwnerRepository,
    draft_id: str,
    *,
    for_update: bool = False,
) -> OwnedPolicyDraft | None:
    return repository.draft_by_id(draft_id, for_update=for_update)


def update_policy_draft(
    repository: IdentityPolicyOwnerRepository,
    draft_id: str,
    *,
    expected_revision: int,
    content: dict[str, Any],
    content_hash: str,
    stale: bool,
    now: datetime,
) -> OwnedPolicyDraft | None:
    return repository.update_draft(
        draft_id,
        expected_revision=expected_revision,
        content=content,
        content_hash=content_hash,
        stale=stale,
        now=now,
    )


def save_policy_draft_validation(
    repository: IdentityPolicyOwnerRepository,
    draft_id: str,
    *,
    expected_revision: int,
    evidence: dict[str, Any],
    dependency_versions: dict[str, Any],
    now: datetime,
) -> OwnedPolicyDraft | None:
    return repository.save_validation(
        draft_id,
        expected_revision=expected_revision,
        evidence=evidence,
        dependency_versions=dependency_versions,
        now=now,
    )


def save_policy_draft_preview(
    repository: IdentityPolicyOwnerRepository,
    draft_id: str,
    *,
    expected_revision: int,
    evidence: dict[str, Any],
    dependency_versions: dict[str, Any],
) -> OwnedPolicyDraft | None:
    return repository.save_preview(
        draft_id,
        expected_revision=expected_revision,
        evidence=evidence,
        dependency_versions=dependency_versions,
    )


def preview_policy_candidate(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[OwnedPolicyPreviewItem]:
    policy_catalog(repository, namespace)
    issues, _policy = validate_and_materialize_identity_policy(
        IDENTITY_POLICY_SCHEMA_REVISION,
        after,
    )
    if issues:
        raise OwnedPolicySnapshotUnavailable(namespace)
    return identity_policy_preview(before, after)


def _active_policy_and_effective(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
    *,
    for_update: bool = False,
) -> tuple[OwnedPolicySnapshot, EffectiveIdentityPolicy]:
    snapshot = repository.active_snapshot(namespace, for_update=for_update)
    if snapshot is None:
        raise OwnedPolicySnapshotUnavailable(namespace)
    canonical = json.dumps(
        snapshot.values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not hashlib.sha256(canonical).hexdigest() == snapshot.snapshot_hash:
        raise OwnedPolicySnapshotUnavailable(namespace)
    if validate_identity_policy_catalog(repository.catalog(namespace)):
        raise OwnedPolicySnapshotUnavailable(namespace)
    issues, policy = validate_and_materialize_identity_policy(
        snapshot.schema_revision,
        snapshot.values,
    )
    if issues or policy is None:
        raise OwnedPolicySnapshotUnavailable(namespace)
    return snapshot, policy


def active_policy_snapshot(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
) -> OwnedPolicySnapshot:
    return _active_policy_and_effective(repository, namespace)[0]


def locked_active_policy_snapshot(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
) -> OwnedPolicySnapshot:
    return _active_policy_and_effective(repository, namespace, for_update=True)[0]


def active_policy_archive_settings(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
) -> tuple[OwnedPolicySnapshot, timedelta]:
    snapshot, policy = _active_policy_and_effective(repository, namespace)
    return snapshot, policy.draft_archive_after


def policy_version_snapshot(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
    scope: str,
    version: int,
) -> OwnedPolicySnapshot | None:
    snapshot = repository.version_snapshot(namespace, scope, version)
    if snapshot is None:
        return None
    canonical = json.dumps(
        snapshot.values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != snapshot.snapshot_hash:
        raise OwnedPolicySnapshotUnavailable(namespace)
    if validate_identity_policy_catalog(repository.catalog(namespace)):
        raise OwnedPolicySnapshotUnavailable(namespace)
    issues, policy = validate_and_materialize_identity_policy(
        snapshot.schema_revision,
        snapshot.values,
    )
    if issues or policy is None:
        raise OwnedPolicySnapshotUnavailable(namespace)
    return snapshot


def list_policy_versions(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
    scope: str,
    *,
    before_version: int | None,
    limit: int,
) -> list[OwnedPublishedPolicyVersion]:
    policy_catalog(repository, namespace)
    versions = repository.list_versions(
        namespace,
        scope,
        before_version=before_version,
        limit=limit,
    )
    for version in versions:
        canonical = json.dumps(
            version.snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        issues, policy = validate_and_materialize_identity_policy(
            version.schema_revision,
            version.snapshot,
        )
        if (
            hashlib.sha256(canonical).hexdigest() != version.snapshot_hash
            or issues
            or policy is None
        ):
            raise OwnedPolicySnapshotUnavailable(namespace)
    return versions


def publish_policy_version(
    repository: IdentityPolicyOwnerRepository,
    **values: Any,
) -> OwnedPublishedPolicyVersion | None:
    return repository.publish_version(**values)


def archive_policy_candidates(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
    scope: str,
    *,
    cutoff: datetime,
    limit: int,
) -> list[OwnedPolicyDraft]:
    return repository.archive_candidates(
        namespace,
        scope,
        cutoff=cutoff,
        limit=limit,
    )


def archive_policy_draft(
    repository: IdentityPolicyOwnerRepository,
    **values: Any,
) -> bool:
    return repository.archive_draft(**values)


def validate_policy_candidate(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
    *,
    schema_revision: int,
    values: dict[str, Any],
) -> list[OwnedPolicyValidationIssue]:
    catalog_issues = validate_identity_policy_catalog(repository.catalog(namespace))
    if catalog_issues:
        return catalog_issues
    issues, _policy = validate_and_materialize_identity_policy(schema_revision, values)
    return issues


def effective_identity_policy(
    repository: IdentityPolicyOwnerRepository,
    namespace: str = "identity",
) -> EffectiveIdentityPolicy:
    return _active_policy_and_effective(repository, namespace)[1]
