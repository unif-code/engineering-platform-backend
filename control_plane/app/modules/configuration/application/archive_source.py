import re
from datetime import datetime
from typing import Any

from control_plane.app.modules.configuration.application.archive_timing import _summary, _utc
from control_plane.app.modules.configuration.domain import (
    DraftNotFound,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.domain.archive_source import (
    DraftArchiveSource,
    InactivityArchiveRecord,
    ManualArchiveRecord,
    StoredArchiveEvent,
)
from control_plane.app.modules.configuration.domain.draft_directory import DraftSummary
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort

_COMMON = {"draftId", "namespace", "scope", "result"}
_MANUAL = _COMMON | {"source", "actorId", "actorType", "draftRevision"}
_INACTIVITY = _COMMON | {"policyVersion", "cutoff"}


def _record(
    raw: dict[str, Any], draft: DraftSummary
) -> ManualArchiveRecord | InactivityArchiveRecord:
    event = StoredArchiveEvent.model_validate(raw)
    payload = event.payload
    if (
        draft.archived_at is None
        or event.namespace != draft.namespace
        or event.scope != draft.scope
        or event.aggregate_id != f"{draft.namespace}:{draft.scope}:draft:{draft.id}"
        or _utc(event.occurred_at) != _utc(draft.archived_at)
        or any(
            payload.get(key) != value
            for key, value in {
                "draftId": draft.id,
                "namespace": draft.namespace,
                "scope": draft.scope,
                "result": "ARCHIVED",
            }.items()
        )
    ):
        raise ValueError("Invalid archive event binding")
    if set(payload) == _MANUAL:
        actor = payload["actorId"]
        if (
            payload["source"] != "MANUAL"
            or payload["actorType"] != "human"
            or not isinstance(actor, str)
            or not actor.strip()
            or actor != draft.owner_id
            or type(payload["draftRevision"]) is not int
            or payload["draftRevision"] != draft.revision
        ):
            raise ValueError("Invalid manual archive source")
        return ManualArchiveRecord(
            source="MANUAL",
            id=event.id,
            recorded_at=event.occurred_at,
            actor_id=actor,
            actor_type="human",
            draft_revision=draft.revision,
        )
    if (
        set(payload) != _INACTIVITY
        or type(payload["policyVersion"]) is not int
        or payload["policyVersion"] < 1
    ):
        raise ValueError("Unknown archive source shape")
    value = payload["cutoff"]
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("Unrepresentable cutoff")
    cutoff = datetime.fromisoformat(value)
    if not _utc(draft.last_meaningful_activity_at) <= _utc(cutoff) <= _utc(event.occurred_at):
        raise ValueError("Invalid inactivity archive cutoff")
    return InactivityArchiveRecord(
        source="INACTIVITY",
        id=event.id,
        recorded_at=event.occurred_at,
        policy_version=payload["policyVersion"],
        cutoff=cutoff,
    )


def archive_source(
    owner: PolicyOwnerPort, *, namespace: str, draft_id: str, expected_revision: int
) -> DraftArchiveSource:
    try:
        before = _summary(owner.draft_summary(namespace, "PLATFORM", draft_id), namespace, draft_id)
        if before.revision != expected_revision:
            raise StaleDraftRevision("Draft revision changed")
        rows = owner.archive_records(namespace, "PLATFORM", draft_id)
        try:
            after = _summary(
                owner.draft_summary(namespace, "PLATFORM", draft_id), namespace, draft_id
            )
        except DraftNotFound:
            raise StaleDraftRevision("Draft metadata changed") from None
        # A real same-revision archival can occur between ordinary reads. Resolve it before
        # judging event/state consistency; no current Policy or candidate contents are needed.
        if before.model_dump(mode="json") != after.model_dump(mode="json"):
            raise StaleDraftRevision("Draft metadata changed")
        if (
            (before.status == "DRAFT" and before.archived_at is not None)
            or (
                before.status == "ARCHIVED"
                and (
                    before.archived_at is None
                    or _utc(before.archived_at) < _utc(before.last_meaningful_activity_at)
                )
            )
            or type(rows) is not list
            or len(rows) > 1
            or (before.status == "DRAFT" and rows)
        ):
            raise ValueError("Invalid archive source facts")
        record = _record(rows[0], before) if rows else None
        return DraftArchiveSource(
            draft_id=before.id,
            namespace=namespace,
            scope="PLATFORM",
            draft_revision=before.revision,
            status=before.status,
            archived_at=before.archived_at,
            record_status="NOT_ARCHIVED"
            if before.status == "DRAFT"
            else "RECORDED"
            if record
            else "UNKNOWN",
            record=record,
        )
    except (DraftNotFound, StaleDraftRevision):
        raise
    except Exception:
        raise PolicySnapshotUnavailable("Draft archive source unavailable") from None
