"""One metadata observation, bounded by Current but not a frozen collection of drafts."""

import re

from control_plane.app.modules.configuration.application.base_comparison import _check_snapshot
from control_plane.app.modules.configuration.domain import (
    InvalidPolicyValue,
    PolicySnapshot,
    PolicySnapshotUnavailable,
    StaleDraftBase,
)
from control_plane.app.modules.configuration.domain.draft_directory import (
    DRAFT_ID_PATTERN,
    DraftDirectoryOwner,
    DraftDirectoryView,
    DraftList,
    DraftListItem,
    DraftSummary,
)
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort


def _current(owner: PolicyOwnerPort, namespace: str) -> PolicySnapshot:
    value = _check_snapshot(owner.active_snapshot(namespace), namespace, 1)
    if type(value.version) is not int or type(value.schema_revision) is not int:
        raise PolicySnapshotUnavailable("Draft directory Current unavailable")
    return value


def draft_directory(
    repository: PolicyOwnerPort,
    *,
    namespace: str,
    actor_id: str,
    view: DraftDirectoryView = "ALL",
    owner: DraftDirectoryOwner = "ALL",
    limit: int = 50,
    cursor: str | None = None,
    current_version: int | None = None,
) -> DraftList:
    if (
        view not in ("ALL", "ACTIVE", "STALE", "ARCHIVED")
        or owner not in ("ALL", "MINE")
        or type(limit) is not int
        or not 1 <= limit <= 100
        or not isinstance(actor_id, str)
        or not actor_id
        or (
            current_version is not None
            and (type(current_version) is not int or current_version < 1)
        )
        or (
            cursor is not None
            and (
                not isinstance(cursor, str)
                or re.fullmatch(DRAFT_ID_PATTERN, cursor) is None
                or current_version is None
            )
        )
    ):
        raise InvalidPolicyValue("Invalid draft directory query")
    try:
        before = _current(repository, namespace)
        if current_version is not None and current_version != before.version:
            raise StaleDraftBase("Draft directory Current changed")
        rows = repository.list_draft_summaries(
            namespace,
            "PLATFORM",
            view=view,
            owner_id=actor_id if owner == "MINE" else None,
            current_version=before.version,
            after_id=cursor,
            limit=limit + 1,
        )
        after = _current(repository, namespace)
        # A new publication can explain new Base values. Resolve that race before row validation.
        if after.version != before.version:
            raise StaleDraftBase("Draft directory Current changed")
        if (
            after.schema_revision != before.schema_revision
            or after.snapshot_hash != before.snapshot_hash
        ):
            raise ValueError("Current immutable facts differ at the same version")
        if len(rows) > limit + 1:
            raise ValueError("Oversized draft directory page")
        items = []
        lower = cursor
        for raw in rows:
            row = DraftSummary.model_validate(raw)
            behind = row.base_version < before.version
            matches = (
                view == "ALL"
                or (view == "ARCHIVED" and row.status == "ARCHIVED")
                or (
                    row.status == "DRAFT"
                    and ((view == "ACTIVE" and not behind) or (view == "STALE" and behind))
                )
            )
            if (
                (owner == "MINE" and row.owner_id != actor_id)
                or not matches
                or (lower is not None and row.id <= lower)
            ):
                raise ValueError("Invalid draft directory metadata")
            lower = row.id
            items.append(draft_list_item(row, namespace=namespace, current_version=before.version))
        return DraftList(
            namespace=namespace,
            scope="PLATFORM",
            view=view,
            owner=owner,
            current_version=before.version,
            items=items[:limit],
            next_cursor=items[limit - 1].id if len(items) > limit else None,
        )
    except StaleDraftBase:
        raise
    except Exception:
        raise PolicySnapshotUnavailable("Draft directory unavailable") from None


def draft_list_item(row: DraftSummary, *, namespace: str, current_version: int) -> DraftListItem:
    if (
        row.namespace != namespace
        or row.schema_revision != 1
        or row.base_version > current_version
        or (row.status == "DRAFT" and row.archived_at is not None)
        or (
            row.status == "ARCHIVED"
            and (row.archived_at is None or row.archived_at < row.last_meaningful_activity_at)
        )
    ):
        raise ValueError("Invalid draft metadata")
    return DraftListItem(**row.model_dump(), base_behind=row.base_version < current_version)
