import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.domain import (
    DraftArchived,
    DraftAuthorizationDenied,
    DraftNotFound,
    DraftOwnerRequired,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.domain.draft_archive import DraftArchive
from control_plane.app.modules.configuration.ports.draft_authorization import DraftAuthorizationPort
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort
from control_plane.app.shared.api.request_id import current_request_id


def archive_draft(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    actor_id: str,
    expected_revision: int,
    raw_session: str,
    authorization: DraftAuthorizationPort | None,
    dependencies: ConfigurationDependencies,
) -> DraftArchive:
    if authorization is None:
        raise PolicySnapshotUnavailable("Current archive authorization unavailable")
    draft = owner.draft(draft_id, for_update=True)
    try:
        authorization.check(raw_session=raw_session, actor_id=actor_id)
    except (DraftAuthorizationDenied, PolicySnapshotUnavailable):
        raise
    except Exception:
        raise PolicySnapshotUnavailable("Current archive authorization unavailable") from None
    if draft is None or draft.id != draft_id or draft.namespace != namespace:
        raise DraftNotFound("Draft not found")
    if draft.revision != expected_revision:
        raise StaleDraftRevision("Draft revision changed")
    if draft.owner_id != actor_id:
        raise DraftOwnerRequired("Draft owner required")
    if draft.status == "ARCHIVED":
        raise DraftArchived("Draft archived")
    if draft.scope != "PLATFORM" or draft.status != "DRAFT":
        raise PolicySnapshotUnavailable("Invalid archive target")
    try:
        now = dependencies.clock.now()
        activity = draft.last_meaningful_activity_at
        if (
            not isinstance(now, datetime)
            or (offset := now.utcoffset()) is None
            or offset % timedelta(minutes=1)
            or activity.utcoffset() is None
            or now.astimezone(UTC) < activity.astimezone(UTC)
        ):
            raise ValueError("Unrepresentable archive time")
        receipt = DraftArchive(
            draft_id=draft.id,
            namespace=namespace,
            scope="PLATFORM",
            draft_revision=draft.revision,
            status="ARCHIVED",
            archived_at=now,
        )
    except Exception:
        raise PolicySnapshotUnavailable("Archive time unavailable") from None
    payload = {
        "draftId": draft.id,
        "namespace": namespace,
        "scope": draft.scope,
        "draftRevision": draft.revision,
        "source": "MANUAL",
        "actorId": actor_id,
        "actorType": "human",
        "result": "ARCHIVED",
    }
    if not owner.archive_draft(
        draft_id=draft.id,
        namespace=namespace,
        scope=draft.scope,
        expected_revision=draft.revision,
        expected_owner_id=actor_id,
        expected_activity=activity,
        archived_at=now,
        outbox_id=str(dependencies.random.uuid4()),
        aggregate_id=f"{namespace}:{draft.scope}:draft:{draft.id}",
        outbox_payload=payload,
    ):
        raise StaleDraftRevision("Archive target changed")
    record_in_transaction(
        db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=now,
            actor=actor_id,
            actor_type="human",
            action="configuration.draft.archived",
            target_type="configuration_draft",
            target_id=draft.id,
            result="SUCCESS",
            reason=json.dumps(
                {
                    "namespace": namespace,
                    "scope": draft.scope,
                    "source": "MANUAL",
                    "draftRevision": draft.revision,
                    "archivedAt": now.isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.audit,
    )
    return receipt
