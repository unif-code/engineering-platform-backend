import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    RepositoryAuthorizationState,
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


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


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
        state=WebhookInboxState(row["state"]),
        last_error_code=row["last_error_code"],
        received_at=row["received_at"],
        updated_at=row["updated_at"],
        processed_at=row["processed_at"],
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
    except (WebhookReplayRejected, WebhookSignatureInvalid):
        raise
    except Exception as error:
        raise WebhookSignatureInvalid("Standard Webhook verification is unavailable") from error
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WebhookPayloadInvalid("Webhook payload is invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("project"), dict):
        raise WebhookPayloadInvalid("Webhook payload is invalid")
    project_id = payload["project"].get("id")
    if not isinstance(project_id, (str, int)) or str(project_id) != repository_row["project_id"]:
        raise WebhookPayloadInvalid("Webhook project does not match the repository")
    event_type = normalized_headers.get("x-gitlab-event", "Unknown")
    object_kind = _optional_text(payload, "object_kind")
    state = (
        WebhookInboxState.RECEIVED
        if event_type == "Push Hook" and object_kind == "push"
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
            event_type=event_type,
            object_kind=object_kind,
            project_id=str(project_id),
            ref=_optional_text(payload, "ref"),
            before_sha=_optional_text(payload, "before"),
            after_sha=_optional_text(payload, "after"),
            checkout_sha=_optional_text(payload, "checkout_sha"),
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
    if conflict:
        raise WebhookIdConflict("Webhook ID conflicts with a prior payload")
    return _webhook_dto(inserted)
