import hashlib
import json
from datetime import datetime
from typing import Any

from control_plane.app.modules.identity.domain.configuration_policy import (
    OwnedPolicyDraft,
    OwnedPolicyKey,
    OwnedPolicySnapshot,
    OwnedPolicySnapshotUnavailable,
)
from control_plane.app.modules.identity.ports.configuration_policy import (
    IdentityPolicyOwnerRepository,
)


def policy_catalog(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
) -> list[OwnedPolicyKey]:
    return repository.catalog(namespace)


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


def active_policy_snapshot(
    repository: IdentityPolicyOwnerRepository,
    namespace: str,
) -> OwnedPolicySnapshot:
    snapshot = repository.active_snapshot(namespace)
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
    return snapshot
