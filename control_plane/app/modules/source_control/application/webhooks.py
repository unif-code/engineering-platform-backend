import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application._webhook_summary import (
    parse_safe_webhook_summary,
)
from control_plane.app.modules.source_control.application.audit import (
    append_lifecycle_audit,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    RepositoryAuthorizationState,
    SourceControlDependencyUnavailable,
    VerifiedStandardWebhook,
    WebhookIdConflict,
    WebhookInboxDto,
    WebhookInboxState,
    WebhookPayloadInvalid,
    WebhookReplayRejected,
    WebhookSignatureInvalid,
)


def verify_gitlab_standard_webhook(
    *,
    signing_token: str,
    webhook_id: str,
    timestamp: str,
    signature_header: str,
    raw_body: bytes,
    now: datetime,
    replay_window: timedelta,
) -> VerifiedStandardWebhook:
    if not webhook_id or not timestamp or not signature_header:
        raise WebhookSignatureInvalid("Standard Webhook signature headers are required")
    if not signing_token.startswith("whsec_"):
        raise WebhookSignatureInvalid("Standard Webhook signing token is invalid")
    try:
        timestamp_value = int(timestamp)
        if str(timestamp_value) != timestamp:
            raise ValueError
        signing_key = base64.b64decode(signing_token.removeprefix("whsec_"), validate=True)
    except (ValueError, binascii.Error):
        raise WebhookSignatureInvalid("Standard Webhook signature metadata is invalid") from None
    if not signing_key:
        raise WebhookSignatureInvalid("Standard Webhook signing token is invalid")
    occurred_at = datetime.fromtimestamp(timestamp_value, tz=UTC)
    if abs(now - occurred_at) > replay_window:
        raise WebhookReplayRejected("Standard Webhook timestamp is outside the replay window")
    message = webhook_id.encode() + b"." + timestamp.encode("ascii") + b"." + raw_body
    expected = "v1," + base64.b64encode(
        hmac.new(signing_key, message, hashlib.sha256).digest()
    ).decode("ascii")
    valid = any(
        value and hmac.compare_digest(expected, value) for value in signature_header.split(" ")
    )
    if not valid:
        raise WebhookSignatureInvalid("Standard Webhook signature is invalid")
    return VerifiedStandardWebhook(webhook_id=webhook_id, timestamp=occurred_at)


def _webhook_dto(row: Any) -> WebhookInboxDto:
    return WebhookInboxDto(
        id=str(row["id"]),
        repository_id=str(row["repository_id"]),
        webhook_id=row["webhook_id"],
        webhook_timestamp=row["webhook_timestamp"],
        payload_digest=row["payload_digest"],
        provider_event_uuid=row["provider_event_uuid"],
        event_type=row["event_type"],
        object_kind=row["object_kind"],
        project_id=row["project_id"],
        ref=row["ref"],
        before_sha=row["before_sha"],
        after_sha=row["after_sha"],
        checkout_sha=row["checkout_sha"],
        mr_iid=row["mr_iid"],
        mr_action=row["mr_action"],
        source_branch=row["source_branch"],
        target_branch=row["target_branch"],
        mr_state=row["mr_state"],
        old_head_sha=row["old_head_sha"],
        head_sha=row["head_sha"],
        state=WebhookInboxState(row["state"]),
        last_error_code=row["last_error_code"],
        received_at=row["received_at"],
        updated_at=row["updated_at"],
        processed_at=row["processed_at"],
    )


def _audit_webhook_rejection(
    *,
    repository_id: str,
    reason: str,
    dependencies: SourceControlDependencies,
) -> None:
    with dependencies.engine.begin() as db:
        append_lifecycle_audit(
            dependencies.repository_factory(db),
            action="source_control.webhook.rejected",
            target_type="workspace_repository",
            target_id=repository_id,
            dependencies=dependencies,
            result="DENIED",
            reason=reason,
            correlation_id=f"source-control:webhook:{repository_id}",
        )


def ingest_signed_gitlab_webhook(
    *,
    repository_id: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    dependencies: SourceControlDependencies,
) -> WebhookInboxDto:
    policy = dependencies.policy
    secrets = dependencies.webhook_secrets
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    with dependencies.engine.connect() as db:
        repository_row = dependencies.repository_factory(db).workspace_repository(repository_id)
    if (
        repository_row is None
        or repository_row["status"] != RepositoryAuthorizationState.AUTHORIZED.value
        or repository_row["webhook_signing_secret_ref"] is None
        or policy is None
        or secrets is None
    ):
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_VERIFICATION_UNAVAILABLE",
            dependencies=dependencies,
        )
        raise WebhookSignatureInvalid("Standard Webhook verification is unavailable")
    try:
        signing_token = secrets.resolve(repository_row["webhook_signing_secret_ref"])
        verified = verify_gitlab_standard_webhook(
            signing_token=signing_token,
            webhook_id=normalized_headers.get("webhook-id", ""),
            timestamp=normalized_headers.get("webhook-timestamp", ""),
            signature_header=normalized_headers.get("webhook-signature", ""),
            raw_body=raw_body,
            now=dependencies.clock.now(),
            replay_window=policy.webhook_replay_window(),
        )
    except SourceControlDependencyUnavailable:
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_VERIFICATION_UNAVAILABLE",
            dependencies=dependencies,
        )
        raise
    except WebhookReplayRejected:
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_REPLAY_REJECTED",
            dependencies=dependencies,
        )
        raise
    except WebhookSignatureInvalid:
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_SIGNATURE_INVALID",
            dependencies=dependencies,
        )
        raise
    except Exception as error:
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_VERIFICATION_UNAVAILABLE",
            dependencies=dependencies,
        )
        raise WebhookSignatureInvalid("Standard Webhook verification is unavailable") from error
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_PAYLOAD_INVALID",
            dependencies=dependencies,
        )
        raise WebhookPayloadInvalid("Webhook payload is invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("project"), dict):
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_PAYLOAD_INVALID",
            dependencies=dependencies,
        )
        raise WebhookPayloadInvalid("Webhook payload is invalid")
    project_id = payload["project"].get("id")
    if (
        isinstance(project_id, bool)
        or not isinstance(project_id, (str, int))
        or str(project_id) != repository_row["project_id"]
    ):
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_PROJECT_MISMATCH",
            dependencies=dependencies,
        )
        raise WebhookPayloadInvalid("Webhook project does not match the repository")
    try:
        summary = parse_safe_webhook_summary(
            payload,
            project_id=str(project_id),
            event_header=normalized_headers.get("x-gitlab-event"),
        )
    except WebhookPayloadInvalid:
        _audit_webhook_rejection(
            repository_id=repository_id,
            reason="WEBHOOK_PAYLOAD_INVALID",
            dependencies=dependencies,
        )
        raise
    state = (
        WebhookInboxState.RECEIVED
        if summary.object_kind in {"push", "merge_request"}
        else WebhookInboxState.IGNORED
    )
    now = dependencies.clock.now()
    payload_digest = "sha256:" + hashlib.sha256(raw_body).hexdigest()
    conflict = False
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        inserted = repository.accept_webhook(
            id=str(dependencies.random.uuid4()),
            repository_id=repository_id,
            webhook_id=verified.webhook_id,
            webhook_timestamp=verified.timestamp,
            payload_digest=payload_digest,
            provider_event_uuid=normalized_headers.get("x-gitlab-event-uuid"),
            event_type=summary.event_type,
            object_kind=summary.object_kind,
            project_id=summary.project_id,
            ref=summary.ref,
            before_sha=summary.before_sha,
            after_sha=summary.after_sha,
            checkout_sha=summary.checkout_sha,
            mr_iid=summary.mr_iid,
            mr_action=summary.mr_action,
            source_branch=summary.source_branch,
            target_branch=summary.target_branch,
            mr_state=summary.mr_state,
            old_head_sha=summary.old_head_sha,
            head_sha=summary.head_sha,
            state=state.value,
            processed_at=now if state is WebhookInboxState.IGNORED else None,
            now=now,
        )
        if inserted is None:
            existing = repository.webhook_by_message(repository_id, verified.webhook_id)
            if existing is None:
                raise WebhookIdConflict("Webhook identity could not be resolved")
            if existing["payload_digest"] != payload_digest:
                dependencies.audit.append_in_transaction(
                    db,
                    AuditEnvelope(
                        id=str(dependencies.random.uuid4()),
                        occurred_at=now,
                        actor="SYSTEM",
                        actor_type="SYSTEM",
                        action="source_control.webhook.conflict",
                        target_type="webhook_inbox",
                        target_id=verified.webhook_id,
                        result="DENIED",
                        reason="WEBHOOK_ID_CONFLICT",
                        correlation_id=f"source-control:webhook:{repository_id}",
                    ),
                )
                conflict = True
            else:
                inserted = existing
        else:
            append_lifecycle_audit(
                repository,
                action=(
                    "source_control.webhook.accepted"
                    if state is WebhookInboxState.RECEIVED
                    else "source_control.webhook.ignored"
                ),
                target_type="webhook_inbox",
                target_id=str(inserted["id"]),
                dependencies=dependencies,
                correlation_id=f"source-control:webhook:{repository_id}",
            )
    if conflict:
        raise WebhookIdConflict("Webhook ID conflicts with a prior payload")
    return _webhook_dto(inserted)
