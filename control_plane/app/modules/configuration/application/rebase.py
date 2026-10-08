import json
from copy import deepcopy
from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.configuration.application.base_comparison import (
    _equal,
    compare_draft_base,
)
from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application.drafts import _audit, _content_hash
from control_plane.app.modules.configuration.application.rebase_candidate import rebase_candidate
from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftArchived,
    DraftAuthorizationDenied,
    DraftNotFound,
    DraftOwnerRequired,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.ports.draft_authorization import (
    DraftAuthorizationPort,
)
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort


def apply_draft_rebase(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    actor_id: str,
    expected_revision: int,
    request: dict[str, Any],
    raw_session: str,
    authorization: DraftAuthorizationPort | None,
    dependencies: ConfigurationDependencies,
) -> Draft:
    if authorization is None:
        raise PolicySnapshotUnavailable("Current rebase authorization unavailable")
    current = owner.locked_active_snapshot(namespace)
    draft = owner.draft(draft_id, for_update=True)
    if draft is None or draft.id != draft_id or draft.namespace != namespace:
        raise DraftNotFound("Draft not found")
    if draft.revision != expected_revision:
        raise StaleDraftRevision("Draft revision changed")
    if draft.owner_id != actor_id:
        raise DraftOwnerRequired("Draft owner required")
    if draft.status == "ARCHIVED":
        raise DraftArchived("Draft archived")
    observation = compare_draft_base(
        owner,
        namespace=namespace,
        draft_id=draft_id,
        expected_revision=expected_revision,
        draft=draft,
        current=current,
    )
    for key, value in {
        "baseVersion": observation.base_version,
        "currentVersion": observation.current_version,
        "schemaRevision": observation.schema_revision,
        "baseSnapshotHash": observation.base_snapshot_hash,
        "currentSnapshotHash": observation.current_snapshot_hash,
        "draftContentHash": observation.draft_content_hash,
    }.items():
        if not _equal(request.get(key), value):
            raise StaleDraftRevision("Rebase observation changed")
    if observation.base_version >= observation.current_version:
        raise StaleDraftRevision("Draft does not need rebase")
    resolutions = request.get("resolutions")
    normalized, selections = rebase_candidate(owner, observation, resolutions)
    try:
        authorization.check(raw_session=raw_session, actor_id=actor_id)
    except (DraftAuthorizationDenied, PolicySnapshotUnavailable):
        raise
    except Exception:
        raise PolicySnapshotUnavailable("Current rebase authorization unavailable") from None
    now = dependencies.clock.now()
    updated = owner.rebase_draft(
        draft_id,
        namespace=namespace,
        expected_revision=expected_revision,
        expected_owner_id=actor_id,
        expected_base_version=draft.base_version,
        schema_revision=draft.schema_revision,
        base_version=current.version,
        content=normalized,
        content_hash=_content_hash(normalized),
        now=now,
    )
    if updated is None:
        raise StaleDraftRevision("Draft changed before rebase")
    record_id = str(dependencies.random.uuid4())
    record = dict(
        id=record_id,
        draft_id=draft_id,
        namespace=namespace,
        scope=draft.scope,
        schema_revision=draft.schema_revision,
        actor_id=actor_id,
        recorded_at=now,
        before_revision=draft.revision,
        after_revision=updated.revision,
        base_version=draft.base_version,
        current_version=current.version,
        base_snapshot_hash=observation.base_snapshot_hash,
        current_snapshot_hash=current.snapshot_hash,
        before_content_hash=draft.content_hash,
        after_content_hash=updated.content_hash,
        before_content=deepcopy(draft.content),
        after_content=deepcopy(updated.content),
        selections=selections,
    )
    owner.record_rebase(**record)
    summary = {
        key: value
        for key, value in record.items()
        if key not in {"before_content", "after_content", "recorded_at", "selections"}
    }
    summary["selections"] = {
        key: {"change": value["change"], "source": value["source"]}
        for key, value in selections.items()
    }
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.draft.rebased",
        draft_id=draft_id,
        result="SUCCESS",
        reason=json.dumps(summary, sort_keys=True, separators=(",", ":")),
    )
    return updated
