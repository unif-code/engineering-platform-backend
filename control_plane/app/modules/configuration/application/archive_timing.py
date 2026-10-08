"""An exact inactivity observation, not an archival command or execution promise."""

from datetime import UTC, datetime, timedelta
from typing import Any

from control_plane.app.modules.configuration.application.base_comparison import _check_snapshot
from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application.draft_directory import draft_list_item
from control_plane.app.modules.configuration.domain import (
    DraftNotFound,
    PolicySnapshot,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.domain.archive_timing import DraftArchiveTiming
from control_plane.app.modules.configuration.domain.draft_directory import DraftSummary
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort


def _settings(owner: PolicyOwnerPort, namespace: str) -> tuple[PolicySnapshot, timedelta]:
    snapshot, interval = owner.read_archive_settings(namespace)
    snapshot = _check_snapshot(snapshot, namespace, 1)
    if (
        type(snapshot.version) is not int
        or type(snapshot.schema_revision) is not int
        or type(interval) is not timedelta
        or interval.days < 1
        or interval.seconds
        or interval.microseconds
    ):
        raise ValueError("Invalid Current archive settings")
    return snapshot, interval


def _utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or (offset := value.utcoffset()) is None
        or offset % timedelta(minutes=1)
    ):
        # RFC 3339 cannot preserve a subminute offset; never silently truncate it on the wire.
        raise ValueError("Unrepresentable timestamp")
    return value.astimezone(UTC)


def _summary(raw: dict[str, Any] | None, namespace: str, draft_id: str) -> DraftSummary:
    if raw is None:
        raise DraftNotFound("Draft not found")
    for field, expected in [("id", draft_id), ("namespace", namespace), ("scope", "PLATFORM")]:
        if not isinstance(raw.get(field), str) or not raw[field]:
            raise ValueError("Invalid summary identity")
        if raw[field] != expected:
            raise DraftNotFound("Draft not found")
    summary = DraftSummary.model_validate(raw)
    _utc(summary.last_meaningful_activity_at)
    if summary.archived_at is not None:
        _utc(summary.archived_at)
    return summary


def archive_timing(
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    expected_revision: int,
    dependencies: ConfigurationDependencies,
) -> DraftArchiveTiming:
    try:
        current, interval = _settings(owner, namespace)
        before = _summary(owner.draft_summary(namespace, "PLATFORM", draft_id), namespace, draft_id)
        if before.revision != expected_revision:
            raise StaleDraftRevision("Draft revision changed")
        final_current, final_interval = _settings(owner, namespace)
        if final_current.version != current.version:
            raise StaleDraftRevision("Current policy changed")
        if (
            final_current.schema_revision != current.schema_revision
            or final_current.snapshot_hash != current.snapshot_hash
            or final_interval != interval
        ):
            raise ValueError("Current archive facts changed at same version")
        raw = owner.draft_summary(namespace, "PLATFORM", draft_id)
        try:
            after = _summary(raw, namespace, draft_id)
        except DraftNotFound:
            raise StaleDraftRevision("Draft metadata changed") from None
        if before.model_dump(mode="json") != after.model_dump(mode="json"):
            raise StaleDraftRevision("Draft metadata changed")
        draft = draft_list_item(before, namespace=namespace, current_version=current.version)
        observed = dependencies.clock.now()
        observed_utc = _utc(observed)
        expected = None
        elapsed = None
        if draft.status == "DRAFT":
            # timedelta addition in UTC is an exact duration, including across timezone/DST changes.
            expected_utc = _utc(draft.last_meaningful_activity_at) + interval
            expected = expected_utc.astimezone(draft.last_meaningful_activity_at.tzinfo)
            _utc(expected)
            elapsed = observed_utc >= expected_utc
        return DraftArchiveTiming(
            draft=draft,
            current=current,
            archive_after_days=interval.days,
            observed_at=observed,
            expected_archive_at=expected,
            inactivity_elapsed=elapsed,
        )
    except (DraftNotFound, StaleDraftRevision):
        raise
    except Exception:
        raise PolicySnapshotUnavailable("Draft archive timing unavailable") from None
