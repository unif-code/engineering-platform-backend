from sqlalchemy import Connection

from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application.drafts import _audit, _content_hash
from control_plane.app.modules.configuration.domain import (
    ConfigurationError,
    DraftArchived,
    DraftNotFound,
    DraftOwnerRequired,
    InvalidPolicyValue,
    PolicyVerificationFailed,
    Preview,
    PublishedVersion,
    SourceStale,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.ports import PolicyOwnerPort
from control_plane.app.modules.identity import TotpChallengeFailed, verify_admin_totp


def _reason_code(error: ConfigurationError) -> str:
    if isinstance(error, DraftNotFound):
        return "DRAFT_NOT_FOUND"
    if isinstance(error, DraftOwnerRequired):
        return "DRAFT_OWNER_REQUIRED"
    if isinstance(error, DraftArchived):
        return "DRAFT_ARCHIVED"
    if isinstance(error, StaleDraftRevision):
        return "STALE_REVISION"
    if isinstance(error, SourceStale):
        return "SOURCE_STALE"
    if isinstance(error, PolicyVerificationFailed):
        return "REAUTHENTICATION_FAILED"
    if isinstance(error, InvalidPolicyValue):
        return "INVALID_POLICY"
    return "CONFIGURATION_CONFLICT"


def _audit_denial(
    db: Connection,
    *,
    dependencies: ConfigurationDependencies,
    actor_id: str,
    namespace: str,
    draft_id: str,
    error: ConfigurationError,
) -> None:
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.policy.publish_denied",
        draft_id=draft_id,
        result="DENIED",
        reason=f"namespace={namespace}; reasonCode={_reason_code(error)}",
    )


def publish(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    actor_id: str,
    expected_revision: int,
    reason: str,
    totp_code: str,
    dependencies: ConfigurationDependencies,
) -> PublishedVersion:
    try:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise InvalidPolicyValue("publish reason is required")

        active = owner.locked_active_snapshot(namespace)
        draft = owner.draft(draft_id, for_update=True)
        if draft is None or draft.namespace != namespace:
            raise DraftNotFound(draft_id)
        if draft.owner_id != actor_id:
            raise DraftOwnerRequired(draft_id)
        if draft.status == "ARCHIVED":
            raise DraftArchived(draft_id)
        if draft.revision != expected_revision:
            raise StaleDraftRevision(draft_id)
        if draft.base_version != active.version:
            raise SourceStale(draft_id)
        if draft.content_hash != _content_hash(draft.content):
            raise InvalidPolicyValue("draft content hash does not match its candidate")

        issues = owner.validate_candidate(
            namespace,
            schema_revision=draft.schema_revision,
            values=draft.content,
        )
        if issues:
            raise InvalidPolicyValue("draft candidate is not valid for publication")
        preview_items = owner.preview_candidate(
            namespace,
            before=active.values,
            after=draft.content,
        )
        preview_evidence = Preview(
            draft_id=draft.id,
            revision=draft.revision,
            content_hash=draft.content_hash,
            base_version=draft.base_version,
            items=preview_items,
        ).model_dump(mode="json")
        validation_evidence = {
            "valid": True,
            "issues": [],
            "contentHash": draft.content_hash,
            "schemaRevision": draft.schema_revision,
            "baseVersion": draft.base_version,
        }

        if dependencies.identity is None:
            raise RuntimeError("configuration publish identity dependencies are unavailable")
        try:
            verify_admin_totp(
                db,
                actor_id,
                totp_code,
                purpose="POLICY_PUBLISH",
                dependencies=dependencies.identity,
            )
        except TotpChallengeFailed as exc:
            raise PolicyVerificationFailed(draft_id) from exc

        version = active.version + 1
        now = dependencies.clock.now()
        aggregate_id = f"{namespace}:{active.scope}:{version}"
        published = owner.publish_version(
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
            validation=validation_evidence,
            dependencies={},
            preview=preview_evidence,
            source_draft_id=draft.id,
            outbox_id=str(dependencies.random.uuid4()),
            aggregate_id=aggregate_id,
            outbox_payload={
                "namespace": namespace,
                "scope": active.scope,
                "version": version,
                "snapshotHash": draft.content_hash,
            },
        )
        if published is None:
            raise RuntimeError("active policy pointer changed while holding its owner lock")
    except ConfigurationError as error:
        _audit_denial(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            namespace=namespace,
            draft_id=draft_id,
            error=error,
        )
        raise

    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.policy.published",
        draft_id=f"{namespace}:{published.version}",
        target_type="configuration_policy_version",
        result="SUCCESS",
        reason=(
            f"namespace={namespace}; version={published.version}; "
            f"baseVersion={active.version}; snapshotHash={published.snapshot_hash}; "
            f"reason={normalized_reason}"
        ),
    )
    return published
