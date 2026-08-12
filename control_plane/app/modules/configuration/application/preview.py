from sqlalchemy import Connection

from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application.drafts import (
    _audit,
    _require_editable,
)
from control_plane.app.modules.configuration.domain import (
    InvalidPolicyValue,
    Preview,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.ports import PolicyOwnerPort


def preview(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    actor_id: str,
    expected_revision: int,
    dependencies: ConfigurationDependencies,
) -> Preview:
    draft = _require_editable(
        owner,
        draft_id=draft_id,
        namespace=namespace,
        actor_id=actor_id,
        expected_revision=expected_revision,
    )
    issues = owner.validate_candidate(
        namespace,
        schema_revision=draft.schema_revision,
        values=draft.content,
    )
    if issues:
        raise InvalidPolicyValue("candidate is not valid for impact preview")
    active = owner.active_snapshot(namespace)
    items = owner.preview_candidate(
        namespace,
        before=active.values,
        after=draft.content,
    )
    result = Preview(
        draft_id=draft.id,
        revision=draft.revision,
        content_hash=draft.content_hash,
        base_version=draft.base_version,
        items=items,
    )
    saved = owner.save_preview(
        draft.id,
        expected_revision=expected_revision,
        evidence=result.model_dump(mode="json"),
        dependency_versions={},
    )
    if saved is None:
        raise StaleDraftRevision(draft.id)
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.draft.previewed",
        draft_id=draft.id,
        result="SUCCESS",
        reason=(
            f"namespace={namespace}; revision={draft.revision}; "
            f"contentHash={draft.content_hash}; changedKeys={len(items)}"
        ),
    )
    return result
