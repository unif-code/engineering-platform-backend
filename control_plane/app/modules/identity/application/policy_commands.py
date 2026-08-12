import hashlib
import json
from typing import Any

from control_plane.app.modules.identity.application.common import audit
from control_plane.app.modules.identity.application.configuration_policy import (
    create_policy_draft,
    locked_active_policy_snapshot,
    policy_draft,
    policy_version_snapshot,
    preview_policy_candidate,
    publish_policy_version,
    validate_policy_candidate,
)
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.super_admin import verify_admin_totp
from control_plane.app.modules.identity.domain.errors import (
    SuperAdminPermissionDenied,
    TotpChallengeFailed,
)
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.modules.identity.ports.configuration_policy import (
    IdentityPolicyOwnerRepository,
)
from control_plane.app.modules.identity.ports.repository import IdentityRepository
from control_plane.app.shared.idempotency import IdempotentResponse


class _PolicyCommandDenied(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        title: str,
        reason_code: str,
        code: str | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.status_code = status_code
        self.title = title
        self.reason_code = reason_code
        self.code = code

    def response(self) -> IdempotentResponse:
        body: dict[str, Any] = {"title": self.title, "status": self.status_code}
        if self.code is not None:
            body["code"] = self.code
        return IdempotentResponse(
            status_code=self.status_code,
            body=body,
            is_problem=True,
        )


def _content_hash(content: dict[str, Any]) -> str:
    canonical = json.dumps(
        content,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _configuration_audit(
    repository: IdentityRepository,
    *,
    dependencies: IdentityDependencies,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    result: str,
    reason: str,
) -> None:
    audit(
        repository.db,
        dependencies=dependencies,
        actor=Principal(employee_id=actor_id, name="Policy administrator"),
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=result,
        reason=reason,
    )


def _published_response(value: Any) -> IdempotentResponse:
    return IdempotentResponse(
        status_code=201,
        body={
            "namespace": value.namespace,
            "scope": value.scope,
            "version": value.version,
            "snapshot": value.snapshot,
            "snapshotHash": value.snapshot_hash,
            "publishedBy": value.published_by,
            "reason": value.reason,
            "publishedAt": value.published_at.isoformat(),
            "activatedAt": value.activated_at.isoformat(),
            "schemaRevision": value.schema_revision,
        },
        headers={"ETag": f'"v{value.version}"'},
    )


def _draft_response(value: Any) -> IdempotentResponse:
    return IdempotentResponse(
        status_code=201,
        body={
            "id": value.id,
            "namespace": value.namespace,
            "scope": value.scope,
            "content": value.content,
            "baseVersion": value.base_version,
            "ownerId": value.owner_id,
            "revision": value.revision,
            "status": value.status,
            "stale": value.stale,
            "lastMeaningfulActivityAt": value.last_meaningful_activity_at.isoformat(),
            "archivedAt": (None if value.archived_at is None else value.archived_at.isoformat()),
            "schemaRevision": value.schema_revision,
            "contentHash": value.content_hash,
            "validationEvidence": value.validation_evidence,
            "rollbackFromVersion": value.rollback_from_version,
            "previewEvidence": value.preview_evidence,
        },
        headers={"ETag": f'"v{value.revision}"'},
    )


def publish_policy_command(
    policy_repository: IdentityPolicyOwnerRepository,
    identity_repository: IdentityRepository,
    *,
    actor_id: str,
    namespace: str,
    draft_id: str,
    expected_revision: int,
    reason: str,
    totp_code: str,
    dependencies: IdentityDependencies,
) -> IdempotentResponse:
    try:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise _PolicyCommandDenied(
                status_code=422,
                title="Invalid policy value",
                reason_code="INVALID_POLICY",
            )
        active = locked_active_policy_snapshot(policy_repository, namespace)
        draft = policy_draft(policy_repository, draft_id, for_update=True)
        if draft is None or draft.namespace != namespace:
            raise _PolicyCommandDenied(
                status_code=404,
                title="Draft not found",
                reason_code="DRAFT_NOT_FOUND",
            )
        if draft.owner_id != actor_id:
            raise _PolicyCommandDenied(
                status_code=403,
                title="Draft owner required",
                reason_code="DRAFT_OWNER_REQUIRED",
            )
        if draft.status == "ARCHIVED":
            raise _PolicyCommandDenied(
                status_code=409,
                title="Draft archived",
                reason_code="DRAFT_ARCHIVED",
            )
        if draft.revision != expected_revision:
            raise _PolicyCommandDenied(
                status_code=409,
                title="Stale draft revision",
                reason_code="STALE_REVISION",
            )
        if draft.base_version != active.version:
            raise _PolicyCommandDenied(
                status_code=409,
                title="Source policy is stale",
                reason_code="SOURCE_STALE",
                code="SOURCE_STALE",
            )
        if draft.content_hash != _content_hash(draft.content):
            raise _PolicyCommandDenied(
                status_code=422,
                title="Invalid policy value",
                reason_code="INVALID_POLICY",
            )
        issues = validate_policy_candidate(
            policy_repository,
            namespace,
            schema_revision=draft.schema_revision,
            values=draft.content,
        )
        if issues:
            raise _PolicyCommandDenied(
                status_code=422,
                title="Invalid policy value",
                reason_code="INVALID_POLICY",
            )
        preview_items = preview_policy_candidate(
            policy_repository,
            namespace,
            before=active.values,
            after=draft.content,
        )
        try:
            verify_admin_totp(
                identity_repository,
                actor_id,
                totp_code,
                purpose="POLICY_PUBLISH",
                dependencies=dependencies,
            )
        except (SuperAdminPermissionDenied, TotpChallengeFailed) as exc:
            raise _PolicyCommandDenied(
                status_code=403,
                title="Policy reauthentication failed",
                reason_code="REAUTHENTICATION_FAILED",
                code="REAUTHENTICATION_FAILED",
            ) from exc

        now = dependencies.clock.now()
        version = active.version + 1
        published = publish_policy_version(
            policy_repository,
            namespace=namespace,
            scope=active.scope,
            version=version,
            base_version=active.version,
            snapshot=draft.content,
            changeset={
                "items": [
                    {"key": item.key, "before": item.before, "after": item.after}
                    for item in preview_items
                ],
                "sourceDraftId": draft.id,
            },
            published_by=actor_id,
            reason=normalized_reason,
            now=now,
            schema_revision=draft.schema_revision,
            snapshot_hash=draft.content_hash,
            validation={
                "valid": True,
                "issues": [],
                "contentHash": draft.content_hash,
                "schemaRevision": draft.schema_revision,
                "baseVersion": draft.base_version,
            },
            dependencies={},
            preview={
                "draft_id": draft.id,
                "revision": draft.revision,
                "content_hash": draft.content_hash,
                "base_version": draft.base_version,
                "items": [item.model_dump(mode="json") for item in preview_items],
            },
            source_draft_id=draft.id,
            outbox_id=str(dependencies.random.uuid4()),
            aggregate_id=f"{namespace}:{active.scope}:{version}",
            outbox_payload={
                "namespace": namespace,
                "scope": active.scope,
                "version": version,
                "snapshotHash": draft.content_hash,
            },
        )
        if published is None:
            raise RuntimeError("active policy pointer changed while holding its owner lock")
    except _PolicyCommandDenied as denied:
        _configuration_audit(
            identity_repository,
            dependencies=dependencies,
            actor_id=actor_id,
            action="configuration.policy.publish_denied",
            target_type="configuration_draft",
            target_id=draft_id,
            result="DENIED",
            reason=f"namespace={namespace}; reasonCode={denied.reason_code}",
        )
        return denied.response()

    _configuration_audit(
        identity_repository,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.policy.published",
        target_type="configuration_policy_version",
        target_id=f"{namespace}:{published.version}",
        result="SUCCESS",
        reason=(
            f"namespace={namespace}; version={published.version}; "
            f"baseVersion={active.version}; snapshotHash={published.snapshot_hash}; "
            f"reason={normalized_reason}"
        ),
    )
    return _published_response(published)


def rollback_policy_command(
    policy_repository: IdentityPolicyOwnerRepository,
    identity_repository: IdentityRepository,
    *,
    actor_id: str,
    namespace: str,
    scope: str,
    to_version: int,
    expected_version: int,
    reason: str,
    totp_code: str,
    dependencies: IdentityDependencies,
) -> IdempotentResponse:
    target_id = f"{namespace}:{to_version}"
    try:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise _PolicyCommandDenied(
                status_code=422,
                title="Invalid policy value",
                reason_code="INVALID_POLICY",
            )
        active = locked_active_policy_snapshot(policy_repository, namespace)
        if active.scope != scope or active.version != expected_version:
            raise _PolicyCommandDenied(
                status_code=409,
                title="Source policy is stale",
                reason_code="SOURCE_STALE",
                code="SOURCE_STALE",
            )
        target = policy_version_snapshot(
            policy_repository,
            namespace,
            scope,
            to_version,
        )
        if target is None:
            raise _PolicyCommandDenied(
                status_code=404,
                title="Policy version not found",
                reason_code="VERSION_NOT_FOUND",
            )
        issues = validate_policy_candidate(
            policy_repository,
            namespace,
            schema_revision=active.schema_revision,
            values=target.values,
        )
        if issues:
            raise _PolicyCommandDenied(
                status_code=422,
                title="Invalid policy value",
                reason_code="INVALID_POLICY",
            )
        try:
            verify_admin_totp(
                identity_repository,
                actor_id,
                totp_code,
                purpose="POLICY_ROLLBACK",
                dependencies=dependencies,
            )
        except (SuperAdminPermissionDenied, TotpChallengeFailed) as exc:
            raise _PolicyCommandDenied(
                status_code=403,
                title="Policy reauthentication failed",
                reason_code="REAUTHENTICATION_FAILED",
                code="REAUTHENTICATION_FAILED",
            ) from exc
        content = dict(target.values)
        draft = create_policy_draft(
            policy_repository,
            id=str(dependencies.random.uuid4()),
            namespace=namespace,
            scope=scope,
            content=content,
            base_version=active.version,
            owner_id=actor_id,
            now=dependencies.clock.now(),
            schema_revision=active.schema_revision,
            content_hash=_content_hash(content),
            rollback_from_version=to_version,
        )
    except _PolicyCommandDenied as denied:
        _configuration_audit(
            identity_repository,
            dependencies=dependencies,
            actor_id=actor_id,
            action="configuration.policy.rollback_denied",
            target_type="configuration_policy_version",
            target_id=target_id,
            result="DENIED",
            reason=f"namespace={namespace}; reasonCode={denied.reason_code}",
        )
        return denied.response()

    _configuration_audit(
        identity_repository,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.policy.rollback_draft_created",
        target_type="configuration_draft",
        target_id=draft.id,
        result="SUCCESS",
        reason=(
            f"namespace={namespace}; rollbackFromVersion={to_version}; "
            f"baseVersion={active.version}; reason={normalized_reason}"
        ),
    )
    return _draft_response(draft)
