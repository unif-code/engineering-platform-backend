import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from control_plane.app.modules.source_control.domain import WebhookPayloadInvalid

_MR_ACTIONS = frozenset({"open", "update", "merge", "close", "reopen"})
_MR_STATES = frozenset({"opened", "merged", "closed", "locked"})
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class SafeWebhookSummary:
    event_type: str
    object_kind: str | None
    project_id: str
    ref: str | None = None
    before_sha: str | None = None
    after_sha: str | None = None
    checkout_sha: str | None = None
    mr_iid: int | None = None
    mr_action: str | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    mr_state: str | None = None
    old_head_sha: str | None = None
    head_sha: str | None = None


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WebhookPayloadInvalid("Webhook merge request summary is invalid")
    return value


def _matches_project(value: object, expected: str) -> bool:
    return not isinstance(value, bool) and isinstance(value, (str, int)) and str(value) == expected


def _validate_event_header(*, actual: str | None, expected: str) -> None:
    if actual is not None and actual != expected:
        raise WebhookPayloadInvalid("Webhook event header does not match signed body")


def _merge_request_summary(
    payload: Mapping[str, Any],
    *,
    project_id: str,
    event_header: str | None,
) -> SafeWebhookSummary:
    _validate_event_header(actual=event_header, expected="Merge Request Hook")
    attributes = payload.get("object_attributes")
    if not isinstance(attributes, Mapping):
        raise WebhookPayloadInvalid("Webhook merge request summary is invalid")
    iid = attributes.get("iid")
    if isinstance(iid, bool) or not isinstance(iid, int) or iid < 1:
        raise WebhookPayloadInvalid("Webhook merge request IID is invalid")
    for key in ("source_project_id", "target_project_id"):
        value = attributes.get(key)
        if key not in attributes or not _matches_project(value, project_id):
            raise WebhookPayloadInvalid("Webhook merge request project is invalid")
    action = _required_text(attributes, "action")
    state = _required_text(attributes, "state")
    source_branch = _required_text(attributes, "source_branch")
    target_branch = _required_text(attributes, "target_branch")
    last_commit = attributes.get("last_commit")
    if not isinstance(last_commit, Mapping):
        raise WebhookPayloadInvalid("Webhook merge request head is invalid")
    head_sha = _required_text(last_commit, "id")
    old_head_sha = _optional_text(attributes, "oldrev")
    if (
        action not in _MR_ACTIONS
        or state not in _MR_STATES
        or _SHA_PATTERN.fullmatch(head_sha) is None
        or (old_head_sha is not None and _SHA_PATTERN.fullmatch(old_head_sha) is None)
    ):
        raise WebhookPayloadInvalid("Webhook merge request summary is invalid")
    return SafeWebhookSummary(
        event_type="Merge Request Hook",
        object_kind="merge_request",
        project_id=project_id,
        mr_iid=iid,
        mr_action=action,
        source_branch=source_branch,
        target_branch=target_branch,
        mr_state=state,
        old_head_sha=old_head_sha,
        head_sha=head_sha,
    )


def parse_safe_webhook_summary(
    payload: Mapping[str, Any],
    *,
    project_id: str,
    event_header: str | None,
) -> SafeWebhookSummary:
    object_kind = _optional_text(payload, "object_kind")
    if object_kind == "merge_request":
        return _merge_request_summary(
            payload,
            project_id=project_id,
            event_header=event_header,
        )
    if object_kind == "push":
        _validate_event_header(actual=event_header, expected="Push Hook")
        return SafeWebhookSummary(
            event_type="Push Hook",
            object_kind="push",
            project_id=project_id,
            ref=_optional_text(payload, "ref"),
            before_sha=_optional_text(payload, "before"),
            after_sha=_optional_text(payload, "after"),
            checkout_sha=_optional_text(payload, "checkout_sha"),
        )
    return SafeWebhookSummary(
        event_type=event_header or "Unknown",
        object_kind=object_kind,
        project_id=project_id,
    )


__all__: list[str] = []
