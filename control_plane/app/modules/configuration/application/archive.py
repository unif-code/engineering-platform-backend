from datetime import datetime

from sqlalchemy import Connection

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.ports import PolicyOwnerPort

_ARCHIVE_BATCH_SIZE = 100
_ARCHIVER = "CONFIGURATION_DRAFT_ARCHIVER"


def archive_stale_drafts(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    now: datetime,
    dependencies: ConfigurationDependencies,
    namespace: str = "identity",
    scope: str = "PLATFORM",
) -> int:
    # Identity materializes the version and interval from one active snapshot.
    active, archive_after = owner.active_archive_settings(namespace)
    cutoff = now - archive_after
    archived_count = 0
    while True:
        candidates = owner.archive_candidates(
            namespace,
            scope,
            cutoff=cutoff,
            limit=_ARCHIVE_BATCH_SIZE,
        )
        if not candidates:
            break
        batch_archived = 0
        for draft in candidates:
            aggregate_id = f"{namespace}:{scope}:draft:{draft.id}"
            archived = owner.archive_draft(
                draft_id=draft.id,
                namespace=namespace,
                scope=scope,
                expected_revision=draft.revision,
                expected_owner_id=draft.owner_id,
                expected_activity=draft.last_meaningful_activity_at,
                archived_at=now,
                outbox_id=str(dependencies.random.uuid4()),
                aggregate_id=aggregate_id,
                outbox_payload={
                    "draftId": draft.id,
                    "namespace": namespace,
                    "scope": scope,
                    "policyVersion": active.version,
                    "cutoff": cutoff.isoformat(),
                    "result": "ARCHIVED",
                },
            )
            if not archived:
                continue
            record_in_transaction(
                db,
                AuditEnvelope(
                    id=str(dependencies.random.uuid4()),
                    occurred_at=now,
                    actor=_ARCHIVER,
                    actor_type="workload",
                    action="configuration.draft.archived",
                    target_type="configuration_draft",
                    target_id=draft.id,
                    result="SUCCESS",
                    reason=(
                        f"namespace={namespace}; policyVersion={active.version}; "
                        f"cutoff={cutoff.isoformat()}; result=ARCHIVED"
                    ),
                    correlation_id=str(dependencies.random.uuid4()),
                ),
                dependencies.audit,
            )
            batch_archived += 1
        archived_count += batch_archived
        if batch_archived == 0:
            break
    return archived_count
