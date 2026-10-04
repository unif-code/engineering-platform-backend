import json
from copy import deepcopy
from typing import Literal, cast

from sqlalchemy import Connection

from control_plane.app.modules.configuration.application.base_comparison import compare_draft_base
from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application.drafts import _audit, _content_hash
from control_plane.app.modules.configuration.domain import (
    DraftAuthorizationDenied,
    DraftClone,
    DraftCloneSource,
    DraftNotFound,
    PolicySnapshotUnavailable,
)
from control_plane.app.modules.configuration.ports.draft_authorization import DraftAuthorizationPort
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort


def clone_draft(
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
) -> DraftClone:
    if authorization is None:
        raise PolicySnapshotUnavailable("Current clone authorization unavailable")
    current = owner.locked_active_snapshot(namespace)
    source = owner.draft(draft_id, for_update=True)
    if source is None:
        raise DraftNotFound("Draft not found")
    observation = compare_draft_base(
        owner,
        namespace=namespace,
        draft_id=draft_id,
        expected_revision=expected_revision,
        draft=source,
        current=current,
    )
    # The comparison validates all stored facts; Clone always takes the saved source value.
    content = {item.key: deepcopy(item.draft_value) for item in observation.items}
    try:
        authorization.check(raw_session=raw_session, actor_id=actor_id)
    except (DraftAuthorizationDenied, PolicySnapshotUnavailable):
        raise
    except Exception:
        raise PolicySnapshotUnavailable("Current clone authorization unavailable") from None
    now = dependencies.clock.now()
    created = owner.create_draft(
        id=str(dependencies.random.uuid4()),
        namespace=namespace,
        scope=source.scope,
        content=content,
        base_version=source.base_version,
        owner_id=actor_id,
        now=now,
        schema_revision=source.schema_revision,
        content_hash=_content_hash(content),
        stale=source.base_version < current.version,
        rollback_from_version=source.rollback_from_version,
    )
    provenance = DraftCloneSource(
        draft_id=source.id,
        revision=source.revision,
        owner_id=source.owner_id,
        status=cast(Literal["DRAFT", "ARCHIVED"], source.status),
        base_version=source.base_version,
        schema_revision=source.schema_revision,
        content_hash=source.content_hash,
        rollback_from_version=source.rollback_from_version,
        cloned_from_archived_draft_id=source.id if source.status == "ARCHIVED" else None,
    )
    record = dict(
        id=str(dependencies.random.uuid4()),
        draft_id=created.id,
        namespace=namespace,
        scope=source.scope,
        schema_revision=source.schema_revision,
        actor_id=actor_id,
        recorded_at=now,
        source_draft_id=source.id,
        source_revision=source.revision,
        source_owner_id=source.owner_id,
        source_status=source.status,
        base_version=source.base_version,
        current_version=current.version,
        base_snapshot_hash=observation.base_snapshot_hash,
        current_snapshot_hash=current.snapshot_hash,
        source_content=deepcopy(source.content),
        source_content_hash=source.content_hash,
        content_hash=created.content_hash,
        rollback_from_version=source.rollback_from_version,
        cloned_from_archived_draft_id=provenance.cloned_from_archived_draft_id,
    )
    owner.record_clone(**record)
    summary = {
        key: value for key, value in record.items() if key not in {"source_content", "recorded_at"}
    }
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.draft.cloned",
        draft_id=created.id,
        result="SUCCESS",
        reason=json.dumps(summary, sort_keys=True, separators=(",", ":")),
    )
    return DraftClone(source=provenance, current_version_at_clone=current.version, draft=created)
