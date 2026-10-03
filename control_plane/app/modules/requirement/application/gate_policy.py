"""Protected commands: consume independently, final-check, then commit owner facts."""

from typing import Literal

from sqlalchemy import Connection

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.configuration import (
    ConfigurationDependencies,
    ConfigurationError,
    Draft,
    DraftArchived,
    DraftNotFound,
    DraftOwnerRequired,
    InvalidPolicyValue,
    PolicyVerificationFailed,
    PolicyVersionNotFound,
    SourceStale,
    StaleDraftRevision,
)
from control_plane.app.modules.identity import PolicyReauthBinding, PolicyReauthenticationDenied
from control_plane.app.modules.requirement.domain.gate_policy import (
    NAMESPACE,
    GatePolicy,
    content_hash,
)
from control_plane.app.modules.requirement.ports.gate_policy import (
    GatePolicyRepository,
    PolicyAuthorizationPort,
    PolicyReauthenticationPort,
)
from control_plane.app.shared.idempotency import IdempotentResponse


def _checked_draft(
    repository: GatePolicyRepository, *, actor_id: str, draft_id: str, revision: int
) -> Draft:
    active = repository.active_snapshot(NAMESPACE, for_update=True)
    draft = repository.draft(draft_id, for_update=True)
    if draft is None or draft.namespace != NAMESPACE:
        raise DraftNotFound("Draft not found")
    if draft.revision != revision:
        raise StaleDraftRevision("Draft revision changed")
    if draft.owner_id != actor_id:
        raise DraftOwnerRequired("Draft owner required")
    if draft.status != "DRAFT":
        raise DraftArchived("Draft archived")
    if draft.base_version != active.version or draft.stale:
        raise SourceStale("Policy source changed")
    try:
        policy = GatePolicy.parse(
            draft.content,
            namespace=draft.namespace,
            scope=draft.scope,
            schema_revision=draft.schema_revision,
        )
    except ValueError:
        raise InvalidPolicyValue("Invalid policy") from None
    if content_hash(policy.values()) != draft.content_hash:
        raise InvalidPolicyValue("Invalid content hash")
    expected = {
        "content_hash": draft.content_hash,
        "schema_revision": draft.schema_revision,
        "base_version": draft.base_version,
        "dependency_versions": {},
    }
    validation, preview = draft.validation_evidence, draft.preview_evidence
    if (
        validation is None
        or validation.get("valid") is not True
        or validation.get("issues") != []
        or preview is None
        or any(
            evidence.get(key) != value
            for evidence in (validation, preview)
            for key, value in expected.items()
        )
        or preview.get("draft_id") != draft.id
        or preview.get("revision") != draft.revision
    ):
        raise InvalidPolicyValue("Current validation and preview required")
    items = [
        item.model_dump(mode="json")
        for item in repository.preview_candidate(
            NAMESPACE, before=active.values, after=draft.content
        )
    ]
    if preview.get("items") != items:
        raise InvalidPolicyValue("Preview changed")
    return draft


def _denied(error: ConfigurationError) -> IdempotentResponse:
    if isinstance(error, (DraftNotFound, PolicyVersionNotFound)):
        status = 404
    elif isinstance(error, (DraftOwnerRequired, PolicyVerificationFailed)):
        status = 403
    elif isinstance(error, InvalidPolicyValue):
        status = 422
    else:
        status = 409
    body: dict[str, object] = {"title": "Policy command denied", "status": status}
    if isinstance(error, PolicyVerificationFailed):
        body["code"] = "REAUTHENTICATION_FAILED"
    elif isinstance(error, SourceStale):
        body["code"] = "SOURCE_STALE"
    return IdempotentResponse(status_code=status, body=body, is_problem=True)


def protected_policy_command(
    db: Connection,
    repository: GatePolicyRepository,
    *,
    dependencies: ConfigurationDependencies,
    reauthentication: PolicyReauthenticationPort,
    authorization: PolicyAuthorizationPort,
    operation: Literal["POLICY_PUBLISH", "POLICY_ROLLBACK"],
    actor_id: str,
    namespace: str,
    raw_session: str,
    totp_code: str,
    reason: str,
    attempt_id: str,
    fingerprint: str,
    draft_id: str,
    revision: int,
    scope: str = "PLATFORM",
    to_version: int | None = None,
) -> IdempotentResponse:
    try:
        if namespace != NAMESPACE or scope != "PLATFORM" or not reason.strip():
            raise InvalidPolicyValue("Invalid command")
        active = repository.active_snapshot(NAMESPACE, for_update=True)
        if operation == "POLICY_PUBLISH":
            draft = _checked_draft(
                repository, actor_id=actor_id, draft_id=draft_id, revision=revision
            )
        else:
            if active.version != revision:
                raise SourceStale("Policy source changed")
            target = repository.version_snapshot(NAMESPACE, scope, to_version or 0)
            if target is None:
                raise PolicyVersionNotFound("Historical policy missing")
            draft = Draft(
                id=draft_id,
                namespace=NAMESPACE,
                scope=scope,
                content=target.values,
                base_version=active.version,
                owner_id=actor_id,
                revision=1,
                status="DRAFT",
                stale=False,
                last_meaningful_activity_at=dependencies.clock.now(),
                archived_at=None,
                schema_revision=target.schema_revision,
                content_hash=target.snapshot_hash,
                validation_evidence=None,
                rollback_from_version=to_version,
            )
        before = authorization.check(raw_session=raw_session, actor_id=actor_id)
        binding = PolicyReauthBinding(
            actor_id=actor_id,
            operation=operation,
            namespace=NAMESPACE,
            scope=scope,
            draft_id=draft.id,
            draft_revision=draft.revision,
            content_hash=draft.content_hash,
            schema_revision=draft.schema_revision,
            base_version=draft.base_version,
            dependency_versions=(),
            command_attempt_id=attempt_id,
            request_fingerprint=fingerprint,
        )
        try:
            receipt = reauthentication.verify_and_consume_policy_reauth(
                raw_session=raw_session, totp_code=totp_code, binding=binding, attempt_id=attempt_id
            )
            reauthentication.validate_consumed_policy_reauth(
                raw_session=raw_session, binding=binding, receipt=receipt
            )
        except PolicyReauthenticationDenied:
            raise PolicyVerificationFailed("Reauthentication denied") from None
        if receipt.binding != binding or dependencies.clock.now() >= receipt.expires_at:
            raise PolicyVerificationFailed("Receipt mismatch or expiry")
        if operation == "POLICY_PUBLISH":
            if (
                _checked_draft(repository, actor_id=actor_id, draft_id=draft_id, revision=revision)
                != draft
            ):
                raise SourceStale("Draft changed")
        elif repository.active_snapshot(NAMESPACE, for_update=True) != active:
            raise SourceStale("Policy source changed")
        if authorization.check(raw_session=raw_session, actor_id=actor_id) != before:
            raise PolicyVerificationFailed("Authorization changed")
        # Final decision point; no cross-owner transaction lock is claimed.
        try:
            reauthentication.validate_consumed_policy_reauth(
                raw_session=raw_session, binding=binding, receipt=receipt
            )
        except PolicyReauthenticationDenied:
            raise PolicyVerificationFailed("Reauthentication denied") from None
        now = dependencies.clock.now()
        if now >= receipt.expires_at:
            raise PolicyVerificationFailed("Receipt expired")
        if operation == "POLICY_PUBLISH":
            published = repository.publish(
                draft,
                receipt,
                actor_id=actor_id,
                reason=reason.strip(),
                now=now,
                outbox_id=str(dependencies.random.uuid4()),
            )
            body = published.model_dump(mode="json")
            tag = published.version
        else:
            draft = repository.create_draft(
                id=draft.id,
                namespace=NAMESPACE,
                scope=scope,
                content=draft.content,
                base_version=draft.base_version,
                owner_id=actor_id,
                now=now,
                schema_revision=draft.schema_revision,
                content_hash=draft.content_hash,
                rollback_from_version=to_version,
            )
            repository.reference_receipt(receipt, now=now)
            body = draft.model_dump(mode="json")
            tag = draft.revision
        response = IdempotentResponse(
            status_code=201,
            body={_camel(key): value for key, value in body.items()},
            headers={"ETag": f'"v{tag}"'},
        )
        action = (
            "configuration.policy.published"
            if operation == "POLICY_PUBLISH"
            else "configuration.policy.rollback_draft_created"
        )
        result = "SUCCESS"
    except ConfigurationError as error:
        response = _denied(error)
        action = (
            "configuration.policy.publish_denied"
            if operation == "POLICY_PUBLISH"
            else "configuration.policy.rollback_denied"
        )
        result = "DENIED"
    record_in_transaction(
        db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor_id,
            actor_type="human",
            action=action,
            target_type="configuration_draft",
            target_id=draft_id,
            result=result,
            reason=f"namespace={NAMESPACE}; operation={operation}; result={result}",
            correlation_id=str(dependencies.random.uuid4()),
        ),
        dependencies.audit,
    )
    return response


def _camel(key: str) -> str:
    head, *tail = key.split("_")
    return head + "".join(word.title() for word in tail)
