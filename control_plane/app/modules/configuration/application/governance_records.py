"""Validate persisted governance facts, without consulting today's Active or mutable source."""

import re
from copy import deepcopy
from typing import Any

from control_plane.app.modules.configuration.application.base_comparison import (
    _check_snapshot,
    _equal,
    compare_policy_values,
)
from control_plane.app.modules.configuration.application.drafts import _content_hash
from control_plane.app.modules.configuration.application.rebase_candidate import rebase_candidate
from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftBaseComparison,
    DraftCloneSource,
    DraftNotFound,
    InvalidPolicyValue,
    PolicySnapshot,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.domain.governance_records import (
    DraftCloneRecord,
    DraftGovernanceRecords,
    DraftRebaseRecord,
    DraftRebaseSelection,
    StoredCloneRecord,
    StoredRebaseRecord,
)
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort


def _target(owner: PolicyOwnerPort, namespace: str, draft_id: str, revision: int) -> Draft:
    target = owner.draft(draft_id)
    if target is None or target.namespace != namespace or target.id != draft_id:
        raise DraftNotFound("Draft not found")
    if target.revision != revision:
        raise StaleDraftRevision("Draft revision changed")
    if target.scope != "PLATFORM":
        raise PolicySnapshotUnavailable("Draft governance records unavailable")
    return target


def _references(
    owner: PolicyOwnerPort,
    record: StoredCloneRecord | StoredRebaseRecord,
    namespace: str,
    draft_id: str,
) -> tuple[dict[str, Any], PolicySnapshot, PolicySnapshot]:
    if record.namespace != namespace or record.draft_id != draft_id:
        raise ValueError("Record target mismatch")
    snapshots = []
    for version, digest in (
        (record.base_version, record.base_snapshot_hash),
        (record.current_version, record.current_snapshot_hash),
    ):
        snapshot = _check_snapshot(
            owner.version_snapshot(namespace, "PLATFORM", version),
            namespace,
            record.schema_revision,
        )
        if (
            type(snapshot.version) is not int
            or snapshot.version != version
            or snapshot.snapshot_hash != digest
        ):
            raise ValueError("Record version mismatch")
        snapshots.append(snapshot)
    base, current = snapshots
    if base.version > current.version:
        raise ValueError("Record version order")
    metadata = record.model_dump(
        include={
            "id",
            "draft_id",
            "namespace",
            "scope",
            "schema_revision",
            "actor_id",
            "recorded_at",
        }
    )
    return metadata, base, current


def _clone(
    owner: PolicyOwnerPort, raw: dict[str, Any], namespace: str, draft_id: str
) -> DraftCloneRecord:
    record = StoredCloneRecord.model_validate(raw)
    metadata, base, current = _references(owner, record, namespace, draft_id)
    if (
        record.source_draft_id == draft_id
        or record.cloned_from_archived_draft_id
        != (record.source_draft_id if record.source_status == "ARCHIVED" else None)
        or _content_hash(record.source_content) != record.source_content_hash
    ):
        raise ValueError("Clone provenance mismatch")
    if record.rollback_from_version is not None:
        origin = _check_snapshot(
            owner.version_snapshot(namespace, "PLATFORM", record.rollback_from_version),
            namespace,
            record.schema_revision,
        )
        if origin.version != record.rollback_from_version:
            raise ValueError("Clone rollback reference mismatch")
    items = compare_policy_values(
        owner,
        namespace=namespace,
        schema_revision=record.schema_revision,
        base=base,
        current=current,
        content=record.source_content,
    )
    normalized = {item.key: item.draft_value for item in items}
    if _content_hash(normalized) != record.content_hash:
        raise ValueError("Clone created content mismatch")
    return DraftCloneRecord(
        **metadata,
        source=DraftCloneSource(
            draft_id=record.source_draft_id,
            revision=record.source_revision,
            owner_id=record.source_owner_id,
            status=record.source_status,
            base_version=record.base_version,
            schema_revision=record.schema_revision,
            content_hash=record.source_content_hash,
            rollback_from_version=record.rollback_from_version,
            cloned_from_archived_draft_id=record.cloned_from_archived_draft_id,
        ),
        base_snapshot=base,
        current_snapshot_at_operation=current,
        source_content=deepcopy(record.source_content),
        created_content_hash=record.content_hash,
    )


def _rebase(
    owner: PolicyOwnerPort, raw: dict[str, Any], namespace: str, draft_id: str
) -> DraftRebaseRecord:
    record = StoredRebaseRecord.model_validate(raw)
    metadata, base, current = _references(owner, record, namespace, draft_id)
    if (
        record.after_revision != record.before_revision + 1
        or base.version >= current.version
        or _content_hash(record.before_content) != record.before_content_hash
        or _content_hash(record.after_content) != record.after_content_hash
    ):
        raise ValueError("Rebase revision/content mismatch")
    items = compare_policy_values(
        owner,
        namespace=namespace,
        schema_revision=record.schema_revision,
        base=base,
        current=current,
        content=record.before_content,
    )
    observation = DraftBaseComparison(
        draft_id=draft_id,
        namespace=namespace,
        scope="PLATFORM",
        owner_id=record.actor_id,
        draft_revision=record.before_revision,
        status="DRAFT",
        schema_revision=record.schema_revision,
        base_version=base.version,
        current_version=current.version,
        base_snapshot_hash=base.snapshot_hash,
        current_snapshot_hash=current.snapshot_hash,
        draft_content_hash=record.before_content_hash,
        items=items,
    )
    resolutions = {
        key: value["resolution"]
        for key, value in record.selections.items()
        if "resolution" in value
    }
    result, selections = rebase_candidate(owner, observation, resolutions)
    if (
        not _equal(selections, record.selections)
        or not _equal(result, record.after_content)
        or _content_hash(result) != record.after_content_hash
    ):
        raise ValueError("Rebase decisions/result mismatch")
    return DraftRebaseRecord(
        **metadata,
        before_revision=record.before_revision,
        after_revision=record.after_revision,
        base_snapshot=base,
        current_snapshot_at_operation=current,
        before_content=deepcopy(record.before_content),
        after_content=deepcopy(record.after_content),
        before_content_hash=record.before_content_hash,
        after_content_hash=record.after_content_hash,
        selections={
            key: DraftRebaseSelection.model_validate({"resolution": None, **value})
            for key, value in selections.items()
        },
    )


def governance_records(
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    expected_revision: int,
    limit: int = 50,
    cursor: str | None = None,
) -> DraftGovernanceRecords:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise InvalidPolicyValue("Invalid governance record limit")
    if cursor is not None and (
        not isinstance(cursor, str)
        or not re.fullmatch(r"[1-9][0-9]*", cursor)
        or len(cursor) > len(str(expected_revision))
        or int(cursor) > expected_revision
    ):
        raise InvalidPolicyValue("Invalid governance record cursor")
    before = None if cursor is None else int(cursor)
    try:
        _target(owner, namespace, draft_id, expected_revision)
        raw_clone = owner.clone_record(namespace, "PLATFORM", draft_id)
        cloned = None if raw_clone is None else _clone(owner, raw_clone, namespace, draft_id)
        rows = owner.rebase_records(
            namespace,
            "PLATFORM",
            draft_id,
            through_revision=expected_revision,
            before_revision=before,
            limit=limit + 1,
        )
        if len(rows) > limit + 1:
            raise ValueError("Oversized record page")
        rebases = [_rebase(owner, row, namespace, draft_id) for row in rows]
        upper = expected_revision + 1 if before is None else before
        seen = set()
        for record in rebases:
            if record.after_revision >= upper or record.id in seen:
                raise ValueError("Unordered record page")
            upper = record.after_revision
            seen.add(record.id)
        _target(owner, namespace, draft_id, expected_revision)
        return DraftGovernanceRecords(
            draft_id=draft_id,
            namespace=namespace,
            scope="PLATFORM",
            draft_revision=expected_revision,
            clone_record=cloned,
            rebases=rebases[:limit],
            next_cursor=str(rebases[limit - 1].after_revision) if len(rebases) > limit else None,
        )
    except (DraftNotFound, StaleDraftRevision):
        raise
    except Exception:
        raise PolicySnapshotUnavailable("Draft governance records unavailable") from None
