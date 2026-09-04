from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import control_plane.app.modules.identity as identity


def binding(**changes: Any) -> identity.PolicyReauthBinding:
    assert hasattr(identity, "PolicyReauthBinding"), "exact policy binding is not implemented"
    values: dict[str, Any] = {
        "actor_id": "actor-1",
        "operation": "POLICY_PUBLISH",
        "namespace": "requirement.gate",
        "scope": "PLATFORM",
        "draft_id": "draft-1",
        "draft_revision": 2,
        "content_hash": "a" * 64,
        "schema_revision": 1,
        "base_version": 3,
        "dependency_versions": (("workspace", 2), ("identity", 1)),
        "command_attempt_id": "attempt-1",
        "request_fingerprint": "b" * 64,
    }
    values.update(changes)
    return identity.PolicyReauthBinding(**values)


def test_binding_canonicalizes_dependencies_and_is_deeply_immutable() -> None:
    value = binding()
    reordered = binding(dependency_versions=(("identity", 1), ("workspace", 2)))
    assert value == reordered
    assert value.canonical_hash == reordered.canonical_hash
    assert value.dependency_versions == (("identity", 1), ("workspace", 2))
    with pytest.raises(FrozenInstanceError):
        value.actor_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        value.dependency_versions[0] = ("identity", 5)  # type: ignore[index]
    assert replace(value, operation="POLICY_ROLLBACK").canonical_hash != value.canonical_hash
    assert replace(value, command_attempt_id="attempt-2").canonical_hash != value.canonical_hash
    assert replace(value, request_fingerprint="c" * 64).canonical_hash != value.canonical_hash


@pytest.mark.parametrize(
    "changes",
    [
        {"dependency_versions": {"identity": 1}},
        {"dependency_versions": (["identity", 1],)},
        {"dependency_versions": (("identity", 1), ("identity", 2))},
        {"dependency_versions": (("identity", True),)},
        {"operation": "ADD"},
        {"actor_id": ""},
        {"content_hash": "not-a-hash"},
        {"request_fingerprint": "not-a-hash"},
        {"draft_revision": 0},
        {"schema_revision": True},
        {"base_version": -1},
    ],
)
def test_binding_rejects_ambiguous_or_mutable_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        binding(**changes)


def test_receipt_requires_exact_binding_session_actor_and_five_minute_freshness() -> None:
    value = binding()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    receipt = identity.ConsumedReauthReceipt(
        receipt_id="receipt-1",
        binding=value,
        session_reference="session-1",
        account_version=4,
        consumed_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    assert receipt.matches(value, session_reference="session-1", now=now)
    assert not receipt.matches(value, session_reference="session-2", now=now)
    assert not receipt.matches(
        replace(value, actor_id="other"), session_reference="session-1", now=now
    )
    for change in (
        {"operation": "POLICY_ROLLBACK"},
        {"draft_revision": 3},
        {"draft_id": "other"},
        {"namespace": "identity"},
        {"scope": "OTHER"},
        {"schema_revision": 2},
        {"base_version": 4},
        {"content_hash": "c" * 64},
        {"dependency_versions": (("identity", 2),)},
        {"command_attempt_id": "attempt-2"},
        {"request_fingerprint": "d" * 64},
    ):
        assert replace(value, **change).canonical_hash != value.canonical_hash
        assert not receipt.matches(replace(value, **change), session_reference="session-1", now=now)
    assert not receipt.matches(value, session_reference="session-1", now=now - timedelta(seconds=1))
    assert not receipt.matches(value, session_reference="session-1", now=now + timedelta(minutes=5))
