import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from control_plane.app.modules.configuration.domain.draft_directory import DraftDirectoryId
from control_plane.app.modules.configuration.domain.models import PolicySnapshotUnavailable
from control_plane.app.shared.json import canonical_json, json_digest

Positive = Annotated[int, Field(gt=0)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Operation = Literal["CREATE", "UPDATE", "VALIDATE", "TAKEOVER", "REBASE", "CLONE", "ROLLBACK"]


class _RevisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RevisionValidationIssue(_RevisionModel):
    code: str
    key: str
    message: str


class RevisionValidation(_RevisionModel):
    valid: bool
    issues: list[RevisionValidationIssue]


class RevisionTakeover(_RevisionModel):
    previous_owner_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1000)


class DraftRevision(_RevisionModel):
    revision: Positive
    operation: Operation
    actor_id: str = Field(min_length=1)
    recorded_at: AwareDatetime
    owner_id: str = Field(min_length=1)
    base_version: Positive
    schema_revision: Positive
    content_hash: Digest
    content_json: str
    content_hash_matches_content: bool
    revision_snapshot_hash: Digest
    rollback_from_version: Positive | None
    validation: RevisionValidation | None
    takeover: RevisionTakeover | None
    governance_record_id: DraftDirectoryId | None


class StoredDraftRevision(DraftRevision):
    draft_id: DraftDirectoryId
    namespace: str = Field(min_length=1)
    scope: Literal["PLATFORM"]


class DraftRevisions(_RevisionModel):
    draft_id: DraftDirectoryId
    namespace: str = Field(min_length=1)
    scope: Literal["PLATFORM"]
    draft_revision: Positive
    coverage: Literal["NONE", "PARTIAL", "COMPLETE"]
    recorded_count: int = Field(ge=0)
    first_recorded_revision: Positive | None
    last_recorded_revision: Positive | None
    items: list[DraftRevision]
    next_cursor: str | None


def revision_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def revision_envelope(row: dict[str, Any]) -> dict[str, Any]:
    def public(value: Any) -> Any:
        if isinstance(value, datetime):
            return revision_time(value)
        if isinstance(value, dict):
            return {to_camel(key): public(item) for key, item in value.items()}
        if isinstance(value, list):
            return [public(item) for item in value]
        return value

    return {
        to_camel(key): public(value)
        for key, value in row.items()
        if key != "revision_snapshot_hash"
    }


def checked_revision(raw: dict[str, Any]) -> StoredDraftRevision:
    row = StoredDraftRevision.model_validate(raw)

    def reject_constant(value: str) -> None:
        raise ValueError("Non-finite candidate JSON")

    content = json.loads(row.content_json, parse_constant=reject_constant)
    if not isinstance(content, dict) or canonical_json(content) != row.content_json:
        raise ValueError("Invalid saved candidate JSON")
    matches = hashlib.sha256(row.content_json.encode("utf-8")).hexdigest() == row.content_hash
    if matches != row.content_hash_matches_content:
        raise ValueError("Invalid content hash matching flag")
    if json_digest(revision_envelope(row.model_dump())) != row.revision_snapshot_hash:
        raise ValueError("Invalid revision snapshot hash")
    if (row.operation in {"CREATE", "CLONE", "ROLLBACK"}) != (row.revision == 1):
        raise ValueError("Invalid operation revision")
    if row.actor_id != row.owner_id:
        raise ValueError("Invalid actor/owner binding")
    if (row.validation is not None) != (row.operation == "VALIDATE"):
        raise ValueError("Invalid validation detail")
    if row.validation is not None and row.validation.valid != (not row.validation.issues):
        raise ValueError("Invalid validation outcome")
    if (row.takeover is not None) != (row.operation == "TAKEOVER"):
        raise ValueError("Invalid takeover detail")
    if row.takeover is not None and row.takeover.previous_owner_id == row.owner_id:
        raise ValueError("Invalid previous owner")
    if (row.governance_record_id is not None) != (row.operation in {"CLONE", "REBASE"}):
        raise ValueError("Invalid governance reference")
    if (row.operation == "ROLLBACK" and row.rollback_from_version is None) or (
        row.operation == "CREATE" and row.rollback_from_version is not None
    ):
        raise ValueError("Invalid rollback origin")
    return row


def draft_revision_record(
    draft: dict[str, Any],
    *,
    operation: str,
    actor_id: str,
    validation: dict[str, Any] | None = None,
    takeover: dict[str, Any] | None = None,
    governance_record_id: str | None = None,
) -> dict[str, Any]:
    """Capture a successful owner result; never validate its Policy candidate."""
    try:
        content_json = canonical_json(draft["content"])
        row = {
            key: draft[key]
            for key in (
                "namespace",
                "scope",
                "revision",
                "owner_id",
                "base_version",
                "schema_revision",
                "content_hash",
                "rollback_from_version",
            )
        }
        row.update(
            draft_id=draft["id"],
            operation=operation,
            actor_id=actor_id,
            recorded_at=draft["last_meaningful_activity_at"],
            content_json=content_json,
            content_hash_matches_content=hashlib.sha256(content_json.encode("utf-8")).hexdigest()
            == draft["content_hash"],
            validation=validation,
            takeover=takeover,
            governance_record_id=governance_record_id,
        )
        row["revision_snapshot_hash"] = json_digest(revision_envelope(row))
        return checked_revision(row).model_dump()
    except Exception:
        # This exception must escape safe business-rejection handlers and roll back the owner.
        raise PolicySnapshotUnavailable("Draft revision capture unavailable") from None
