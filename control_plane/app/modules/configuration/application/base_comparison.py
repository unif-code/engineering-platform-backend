"""An observation of three owner snapshots; no mutation or publication evidence."""

from copy import deepcopy
from typing import Any, Literal, cast

from control_plane.app.modules.configuration.application.drafts import _content_hash
from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftBaseChange,
    DraftBaseComparison,
    DraftBaseComparisonItem,
    DraftNotFound,
    InvalidPolicyValue,
    PolicySnapshot,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort


def _equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def _unavailable() -> PolicySnapshotUnavailable:
    return PolicySnapshotUnavailable("Draft base comparison unavailable")


def _check_snapshot(
    snapshot: PolicySnapshot | None, namespace: str, schema_revision: int
) -> PolicySnapshot:
    if (
        snapshot is None
        or snapshot.namespace != namespace
        or snapshot.scope != "PLATFORM"
        or snapshot.version < 1
        or snapshot.schema_revision != schema_revision
        or snapshot.snapshot_hash != _content_hash(snapshot.values)
    ):
        raise _unavailable()
    return snapshot


def compare_draft_base(
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    expected_revision: int,
    draft: Draft | None = None,
    current: PolicySnapshot | None = None,
) -> DraftBaseComparison:
    draft = owner.draft(draft_id) if draft is None else draft
    if draft is None or draft.namespace != namespace or draft.id != draft_id:
        raise DraftNotFound("Draft not found")
    if draft.revision != expected_revision:
        raise StaleDraftRevision("Draft revision changed")
    if (
        draft.scope != "PLATFORM"
        or draft.status not in {"DRAFT", "ARCHIVED"}
        or not draft.owner_id
        or draft.revision < 1
        or draft.base_version < 1
        or draft.schema_revision < 1
        or draft.content_hash != _content_hash(draft.content)
    ):
        raise _unavailable()
    base = _check_snapshot(
        owner.version_snapshot(namespace, draft.scope, draft.base_version),
        namespace,
        draft.schema_revision,
    )
    current = _check_snapshot(
        owner.active_snapshot(namespace) if current is None else current,
        namespace,
        draft.schema_revision,
    )
    if base.version != draft.base_version or current.version < base.version:
        raise _unavailable()
    items = compare_policy_values(
        owner,
        namespace=namespace,
        schema_revision=draft.schema_revision,
        base=base,
        current=current,
        content=draft.content,
    )
    return DraftBaseComparison(
        draft_id=draft.id,
        namespace=namespace,
        scope="PLATFORM",
        owner_id=draft.owner_id,
        draft_revision=draft.revision,
        status=cast(Literal["DRAFT", "ARCHIVED"], draft.status),
        schema_revision=draft.schema_revision,
        base_version=base.version,
        current_version=current.version,
        base_snapshot_hash=base.snapshot_hash,
        current_snapshot_hash=current.snapshot_hash,
        draft_content_hash=draft.content_hash,
        items=items,
    )


def compare_policy_values(
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    schema_revision: int,
    base: PolicySnapshot,
    current: PolicySnapshot,
    content: dict[str, Any],
) -> list[DraftBaseComparisonItem]:
    catalog = owner.catalog(namespace)
    keys = {item.key for item in catalog}
    if (
        not keys
        or len(keys) != len(catalog)
        or any(
            not item.key
            or not item.value_type
            or item.namespace != namespace
            or item.schema_revision != schema_revision
            for item in catalog
        )
    ):
        raise _unavailable()
    try:
        base_values = deepcopy(
            owner.normalize_candidate(
                namespace, schema_revision=base.schema_revision, values=deepcopy(base.values)
            )
        )
        current_values = deepcopy(
            owner.normalize_candidate(
                namespace, schema_revision=current.schema_revision, values=deepcopy(current.values)
            )
        )
    except InvalidPolicyValue:
        raise _unavailable() from None
    draft_values = deepcopy(
        owner.normalize_candidate(
            namespace, schema_revision=schema_revision, values=deepcopy(content)
        )
    )
    if any(set(values) != keys for values in (base_values, current_values, draft_values)):
        raise _unavailable()
    if base.version == current.version and (
        base.snapshot_hash != current.snapshot_hash or not _equal(base_values, current_values)
    ):
        raise _unavailable()
    items = []
    for item in sorted(catalog, key=lambda item: item.key):
        before, active, saved = (
            base_values[item.key],
            current_values[item.key],
            draft_values[item.key],
        )
        current_changed, draft_changed = not _equal(before, active), not _equal(before, saved)
        change: DraftBaseChange = (
            "UNCHANGED"
            if not current_changed and not draft_changed
            else "CURRENT_ONLY"
            if not draft_changed
            else "DRAFT_ONLY"
            if not current_changed
            else "SAME_CHANGE"
            if _equal(active, saved)
            else "CONFLICT"
        )
        items.append(
            DraftBaseComparisonItem(
                key=item.key,
                value_type=item.value_type,
                unit=item.unit,
                base_value=before,
                current_value=active,
                draft_value=saved,
                change=change,
            )
        )
    return items
