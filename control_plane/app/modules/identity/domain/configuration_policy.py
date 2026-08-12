from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.identity.domain.policy import EffectiveIdentityPolicy

IDENTITY_POLICY_SCHEMA_REVISION = 1


@dataclass(frozen=True, slots=True)
class _PolicyDefinition:
    value_type: str
    unit: str | None
    default_value: Any
    min_value: int | None
    max_value: int | None
    enum_values: list[Any] | None
    effect_semantics: str
    impact: str


_POLICY_DEFINITIONS = {
    "identity.temp_credential_ttl": _PolicyDefinition(
        "INTEGER",
        "HOURS",
        24,
        1,
        None,
        None,
        "NEW_OBJECT",
        (
            "Only temporary credentials issued after publication use the new lifetime; "
            "existing credentials keep their recorded expiry."
        ),
    ),
    "identity.password_max_age": _PolicyDefinition(
        "ENUM_OR_INTEGER",
        "DAYS",
        "NEVER",
        1,
        None,
        ["NEVER", 90, 180],
        "IMMEDIATE",
        (
            "New interactive login checks use the new maximum age; stored password "
            "timestamps and running agent attempts are not rewritten."
        ),
    ),
    "identity.session_cap": _PolicyDefinition(
        "INTEGER",
        "SESSIONS",
        3,
        1,
        10,
        None,
        "IMMEDIATE",
        (
            "New session issuance enforces the new cap; existing sessions follow the "
            "explicit session lifecycle and are not silently rewritten."
        ),
    ),
    "identity.session_idle_timeout": _PolicyDefinition(
        "INTEGER",
        "MINUTES",
        60,
        15,
        240,
        None,
        "IMMEDIATE",
        (
            "Authenticated API activity uses the new idle limit immediately; expired "
            "sessions are rejected on their next request."
        ),
    ),
    "identity.login_backoff": _PolicyDefinition(
        "OBJECT",
        None,
        {
            "failureThreshold": 5,
            "initialDelaySeconds": 30,
            "maximumDelaySeconds": 900,
            "resetAfterHours": 24,
        },
        None,
        None,
        None,
        "IMMEDIATE",
        (
            "New authentication attempts use the new backoff policy; existing failure "
            "facts are retained."
        ),
    ),
    "identity.totp_attempt_cap": _PolicyDefinition(
        "INTEGER",
        "ATTEMPTS",
        5,
        1,
        None,
        None,
        "IMMEDIATE",
        (
            "New and active TOTP challenge checks use the new attempt cap without "
            "resetting prior attempts."
        ),
    ),
    "identity.draft_archive_after": _PolicyDefinition(
        "INTEGER",
        "DAYS",
        30,
        1,
        None,
        None,
        "NEXT_SCHEDULE",
        (
            "The next archive task run uses the new inactivity window; drafts are "
            "archived without being deleted."
        ),
    ),
}


class OwnedPolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    namespace: str
    scope: str
    version: int
    schema_revision: int
    snapshot_hash: str
    values: dict[str, Any]


class OwnedPolicyKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    namespace: str
    value_type: str
    unit: str | None
    default_value: Any
    min_value: Any | None
    max_value: Any | None
    enum_values: list[Any] | None
    effect_semantics: str
    schema_revision: int


class OwnedPolicyDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    namespace: str
    scope: str
    content: dict[str, Any]
    base_version: int
    owner_id: str
    revision: int
    status: str
    stale: bool
    last_meaningful_activity_at: datetime
    archived_at: datetime | None
    schema_revision: int
    content_hash: str
    validation_evidence: dict[str, Any] | None
    validation_content_hash: str | None
    validation_schema_revision: int | None
    validation_base_version: int | None
    validation_dependency_versions: dict[str, Any] | None
    rollback_from_version: int | None = None
    preview_evidence: dict[str, Any] | None = None
    preview_content_hash: str | None = None
    preview_schema_revision: int | None = None
    preview_base_version: int | None = None
    preview_dependency_versions: dict[str, Any] | None = None


class OwnedPolicyPreviewItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    before: Any
    after: Any
    effect_semantics: str
    impact: str


class OwnedPublishedPolicyVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    namespace: str
    scope: str
    version: int
    snapshot: dict[str, Any]
    snapshot_hash: str
    published_by: str
    reason: str
    published_at: datetime
    activated_at: datetime
    schema_revision: int


class OwnedPolicySnapshotUnavailable(RuntimeError):
    """The identity-owned active policy cannot be read as a complete snapshot."""


class OwnedPolicyValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    key: str
    message: str


def identity_policy_preview(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[OwnedPolicyPreviewItem]:
    return [
        OwnedPolicyPreviewItem(
            key=key,
            before=before[key],
            after=after[key],
            effect_semantics=_POLICY_DEFINITIONS[key].effect_semantics,
            impact=_POLICY_DEFINITIONS[key].impact,
        )
        for key in sorted(_POLICY_DEFINITIONS)
        if not _exact_json_equal(before[key], after[key])
    ]


def _issue(code: str, key: str, message: str) -> OwnedPolicyValidationIssue:
    return OwnedPolicyValidationIssue(code=code, key=key, message=message)


def _integer_issue(
    key: str,
    value: Any,
    minimum: int | None,
    maximum: int | None,
) -> OwnedPolicyValidationIssue | None:
    if type(value) is not int:
        return _issue(
            "INVALID_TYPE",
            key,
            "Value must use the registered integer type.",
        )
    if minimum is not None and value < minimum:
        return _issue("BELOW_MINIMUM", key, "Value is below the permitted minimum.")
    if maximum is not None and value > maximum:
        return _issue("ABOVE_MAXIMUM", key, "Value exceeds the permitted maximum.")
    return None


def _exact_json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _exact_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _exact_json_equal(left[key], right[key]) for key in left
        )
    return bool(left == right)


def validate_identity_policy_catalog(
    catalog: list[OwnedPolicyKey],
) -> list[OwnedPolicyValidationIssue]:
    by_key = {item.key: item for item in catalog}
    if set(by_key) != set(_POLICY_DEFINITIONS):
        return [
            _issue(
                "SCHEMA_MISMATCH",
                "$schema",
                "Registered policy schema does not match the supported revision.",
            )
        ]
    for key, definition in _POLICY_DEFINITIONS.items():
        item = by_key[key]
        if (
            item.namespace != "identity"
            or item.schema_revision != IDENTITY_POLICY_SCHEMA_REVISION
            or item.value_type != definition.value_type
            or item.unit != definition.unit
            or not _exact_json_equal(item.default_value, definition.default_value)
            or not _exact_json_equal(item.min_value, definition.min_value)
            or not _exact_json_equal(item.max_value, definition.max_value)
            or not _exact_json_equal(item.enum_values, definition.enum_values)
            or item.effect_semantics != definition.effect_semantics
        ):
            return [
                _issue(
                    "SCHEMA_MISMATCH",
                    key,
                    "Registered policy schema does not match the supported revision.",
                )
            ]
    return []


def _validate_identity_policy_candidate_structure(
    schema_revision: int,
    values: dict[str, Any],
) -> list[OwnedPolicyValidationIssue]:
    if schema_revision != IDENTITY_POLICY_SCHEMA_REVISION:
        return [
            _issue(
                "UNSUPPORTED_SCHEMA_REVISION",
                "$schema",
                "Policy schema revision is not supported.",
            )
        ]

    issues: list[OwnedPolicyValidationIssue] = []
    expected_keys = set(_POLICY_DEFINITIONS)
    for key in sorted(expected_keys - set(values)):
        issues.append(_issue("MISSING_KEY", key, "A registered policy value is missing."))
    for key in sorted(set(values) - expected_keys):
        issues.append(_issue("UNREGISTERED_KEY", key, "Policy key is not registered."))
    if issues:
        return issues

    for key, definition in _POLICY_DEFINITIONS.items():
        value = values[key]
        if definition.value_type == "INTEGER":
            issue = _integer_issue(key, value, definition.min_value, definition.max_value)
            if issue is not None:
                issues.append(issue)
        elif definition.value_type == "ENUM_OR_INTEGER" and value != "NEVER":
            issue = _integer_issue(key, value, definition.min_value, definition.max_value)
            if issue is not None:
                issues.append(issue)

    backoff = values["identity.login_backoff"]
    backoff_keys = {
        "failureThreshold",
        "initialDelaySeconds",
        "maximumDelaySeconds",
        "resetAfterHours",
    }
    if not isinstance(backoff, dict) or set(backoff) != backoff_keys:
        issues.append(
            _issue(
                "INVALID_OBJECT",
                "identity.login_backoff",
                "Value does not match the registered object schema.",
            )
        )
    elif any(type(value) is not int or value < 1 for value in backoff.values()):
        issues.append(
            _issue(
                "INVALID_OBJECT",
                "identity.login_backoff",
                "Value does not match the registered object schema.",
            )
        )
    elif backoff["maximumDelaySeconds"] < backoff["initialDelaySeconds"]:
        issues.append(
            _issue(
                "CROSS_FIELD_CONFLICT",
                "identity.login_backoff",
                "Backoff maximum must not be below its initial delay.",
            )
        )
    return sorted(issues, key=lambda issue: (issue.key, issue.code))


class _PolicyMaterializationFailure(Exception):
    def __init__(self, path: str) -> None:
        self.path = path


def _duration(path: str, **parts: int) -> timedelta:
    try:
        return timedelta(**parts)
    except (OverflowError, TypeError) as exc:
        raise _PolicyMaterializationFailure(path) from exc


def identity_policy_from_candidate(values: dict[str, Any]) -> EffectiveIdentityPolicy:
    try:
        return EffectiveIdentityPolicy(
            temp_credential_ttl=_duration(
                "identity.temp_credential_ttl",
                hours=values["identity.temp_credential_ttl"],
            ),
            password_max_age=(
                None
                if values["identity.password_max_age"] == "NEVER"
                else _duration(
                    "identity.password_max_age",
                    days=values["identity.password_max_age"],
                )
            ),
            session_cap=values["identity.session_cap"],
            session_idle_timeout=_duration(
                "identity.session_idle_timeout",
                minutes=values["identity.session_idle_timeout"],
            ),
            backoff_threshold=values["identity.login_backoff"]["failureThreshold"],
            backoff_initial_delay=_duration(
                "identity.login_backoff.initialDelaySeconds",
                seconds=values["identity.login_backoff"]["initialDelaySeconds"],
            ),
            backoff_max_delay=_duration(
                "identity.login_backoff.maximumDelaySeconds",
                seconds=values["identity.login_backoff"]["maximumDelaySeconds"],
            ),
            backoff_reset_after=_duration(
                "identity.login_backoff.resetAfterHours",
                hours=values["identity.login_backoff"]["resetAfterHours"],
            ),
            totp_attempt_cap=values["identity.totp_attempt_cap"],
            draft_archive_after=_duration(
                "identity.draft_archive_after",
                days=values["identity.draft_archive_after"],
            ),
        )
    except _PolicyMaterializationFailure:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise _PolicyMaterializationFailure("$policy") from exc


def validate_and_materialize_identity_policy(
    schema_revision: int,
    values: dict[str, Any],
) -> tuple[list[OwnedPolicyValidationIssue], EffectiveIdentityPolicy | None]:
    issues = _validate_identity_policy_candidate_structure(schema_revision, values)
    if issues:
        return issues, None
    try:
        return [], identity_policy_from_candidate(values)
    except _PolicyMaterializationFailure as error:
        return [
            _issue(
                "UNREPRESENTABLE_VALUE",
                error.path,
                "Value cannot be represented by the runtime policy type.",
            )
        ], None
