import re
from typing import Any

from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftNotFound,
    InvalidPolicyValue,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.domain.draft_directory import DRAFT_ID_PATTERN
from control_plane.app.modules.configuration.domain.draft_revisions import (
    DraftRevision,
    DraftRevisions,
    StoredDraftRevision,
    checked_revision,
    revision_time,
)
from control_plane.app.modules.configuration.domain.governance_records import (
    StoredCloneRecord,
    StoredRebaseRecord,
)
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort
from control_plane.app.shared.json import canonical_json


def capture_revision(owner: PolicyOwnerPort, draft: Draft, **facts: Any) -> None:
    owner.record_revision(draft=draft.model_dump(), **facts)


def _target(owner: PolicyOwnerPort, namespace: str, draft_id: str) -> dict[str, Any]:
    row = owner.draft_revision_target(namespace, "PLATFORM", draft_id)
    if row is None or row.get("id") != draft_id or row.get("namespace") != namespace:
        raise DraftNotFound("Draft not found")
    if (
        set(row) != {"id", "namespace", "scope", "revision"}
        or re.fullmatch(DRAFT_ID_PATTERN, row["id"]) is None
        or row["scope"] != "PLATFORM"
        or type(row["revision"]) is not int
        or row["revision"] < 1
    ):
        raise ValueError("Invalid history target")
    return row


def _reference(owner: PolicyOwnerPort, row: StoredDraftRevision) -> None:
    if row.operation not in {"CLONE", "REBASE"}:
        return
    link: StoredCloneRecord | StoredRebaseRecord
    if row.operation == "CLONE":
        link = StoredCloneRecord.model_validate(
            owner.clone_record(row.namespace, row.scope, row.draft_id)
        )
        base, digest, content, revision = (
            link.base_version,
            link.content_hash,
            link.source_content,
            1,
        )
        if link.rollback_from_version != row.rollback_from_version:
            raise ValueError("Clone rollback origin differs")
    else:
        links = owner.rebase_records(
            row.namespace,
            row.scope,
            row.draft_id,
            through_revision=row.revision,
            before_revision=None,
            limit=2,
        )
        if not links or (len(links) > 1 and links[1].get("after_revision") == row.revision):
            raise ValueError("Missing or duplicate rebase record")
        link = StoredRebaseRecord.model_validate(links[0])
        base, digest, content, revision = (
            link.current_version,
            link.after_content_hash,
            link.after_content,
            link.after_revision,
        )
    if (
        link.id != row.governance_record_id
        or link.draft_id != row.draft_id
        or link.namespace != row.namespace
        or link.scope != row.scope
        or link.actor_id != row.actor_id
        or link.schema_revision != row.schema_revision
        or revision_time(link.recorded_at) != revision_time(row.recorded_at)
        or base != row.base_version
        or digest != row.content_hash
        or revision != row.revision
        or canonical_json(content) != row.content_json
    ):
        raise ValueError("Revision governance binding differs")


def draft_revisions(
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    expected_revision: int,
    limit: int = 50,
    cursor: str | None = None,
) -> DraftRevisions:
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(expected_revision) is not int
        or expected_revision < 1
        or (
            cursor is not None
            and (
                not isinstance(cursor, str)
                or len(cursor) > 20
                or re.fullmatch(r"[1-9][0-9]*", cursor) is None
                or int(cursor) > expected_revision
            )
        )
    ):
        raise InvalidPolicyValue("Invalid revision history query")
    try:
        before = _target(owner, namespace, draft_id)
        if before["revision"] != expected_revision:
            raise StaleDraftRevision("Draft revision changed")
        stats = owner.draft_revision_stats(
            namespace, "PLATFORM", draft_id, through_revision=expected_revision
        )
        rows = owner.draft_revision_rows(
            namespace,
            "PLATFORM",
            draft_id,
            through_revision=expected_revision,
            before_revision=int(cursor) if cursor is not None else None,
            limit=limit + 1,
        )
        try:
            after = _target(owner, namespace, draft_id)
        except DraftNotFound:
            raise StaleDraftRevision("Draft changed") from None
        if after != before:
            raise StaleDraftRevision("Draft revision changed")
        if set(stats) != {"recorded_count", "first_recorded_revision", "last_recorded_revision"}:
            raise ValueError("Invalid revision aggregate")
        count, first, last = (
            stats[key]
            for key in ("recorded_count", "first_recorded_revision", "last_recorded_revision")
        )
        if type(count) is not int or count < 0 or count > expected_revision:
            raise ValueError("Invalid revision count")
        if count == 0:
            if first is not None or last is not None:
                raise ValueError("Invalid empty coverage")
        elif (
            type(first) is not int
            or type(last) is not int
            or not 1 <= first <= last <= expected_revision
            or count > last - first + 1
            or (count == 1 and first != last)
        ):
            raise ValueError("Invalid recorded revision range")
        if not isinstance(rows, list) or len(rows) > limit + 1 or len(rows) > count:
            raise ValueError("Invalid revision page size")
        if cursor is None and (
            len(rows) != min(count, limit + 1) or (rows and rows[0].get("revision") != last)
        ):
            raise ValueError("Page does not match coverage")
        items = []
        upper = int(cursor) if cursor is not None else expected_revision + 1
        for raw in rows:
            row = checked_revision(raw)
            if (
                row.draft_id != draft_id
                or row.namespace != namespace
                or row.scope != "PLATFORM"
                or not first <= row.revision <= last
                or row.revision >= upper
            ):
                raise ValueError("Invalid revision order or binding")
            upper = row.revision
            _reference(owner, row)
            items.append(
                DraftRevision.model_validate(
                    row.model_dump(exclude={"draft_id", "namespace", "scope"})
                )
            )
        if len(rows) <= limit and (
            (rows and rows[-1]["revision"] != first)
            or (not rows and count > 0 and (cursor is None or int(cursor) > first))
        ):
            raise ValueError("Incomplete final page")
        return DraftRevisions(
            draft_id=draft_id,
            namespace=namespace,
            scope="PLATFORM",
            draft_revision=expected_revision,
            coverage="NONE"
            if count == 0
            else "COMPLETE"
            if count == expected_revision and first == 1 and last == expected_revision
            else "PARTIAL",
            **stats,
            items=items[:limit],
            next_cursor=str(items[limit - 1].revision) if len(items) > limit else None,
        )
    except (DraftNotFound, StaleDraftRevision):
        raise
    except Exception:
        raise PolicySnapshotUnavailable("Draft revision history unavailable") from None
