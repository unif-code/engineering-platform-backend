from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from control_plane.app.modules.source_control.domain import (
    AgentDeliveryFact,
    AgentDeliveryFactPayload,
    AgentDeliveryFactTopic,
    AgentExecutionBindingSnapshot,
    AgentPushRequestSpec,
    AgentPushState,
    InvalidAgentPushGrant,
    InvalidAgentPushTransition,
    agent_push_request_fingerprint,
    digest_agent_push_grant,
    transition_agent_push,
    validate_agent_push_expiry,
)

NOW = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)
ATTEMPT_ID = "92000000-0000-0000-0000-000000000301"
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000301"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000301"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000301"
REPOSITORY_ID = "10000000-0000-0000-0000-000000000301"
BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000301"
BRANCH_NAME = "feat/wi-301-source-control"
EXPECTED_HEAD = "a" * 40
TARGET_COMMIT = "b" * 40
CONTENT_DIGEST = f"sha256:{'c' * 64}"
BINDING_DIGEST = f"sha256:{'d' * 64}"


def _spec(**overrides: object) -> AgentPushRequestSpec:
    values: dict[str, object] = {
        "idempotency_key": "agent-push-301",
        "correlation_id": "correlation-301",
        "attempt_id": ATTEMPT_ID,
        "attempt_generation": 3,
        "requirement_id": REQUIREMENT_ID,
        "work_item_id": WORK_ITEM_ID,
        "workspace_id": WORKSPACE_ID,
        "repository_id": REPOSITORY_ID,
        "branch_binding_id": BRANCH_BINDING_ID,
        "branch_name": BRANCH_NAME,
        "expected_remote_head_sha": EXPECTED_HEAD,
        "target_commit_sha": TARGET_COMMIT,
        "content_digest": CONTENT_DIGEST,
        "artifact_refs": ("artifact://patch/301",),
        "expires_at": NOW + timedelta(seconds=60),
    }
    values.update(overrides)
    return AgentPushRequestSpec.model_validate(values)


def _binding(**overrides: object) -> AgentExecutionBindingSnapshot:
    values: dict[str, object] = {
        "attempt_id": ATTEMPT_ID,
        "attempt_generation": 3,
        "execution_binding_digest": BINDING_DIGEST,
        "requirement_id": REQUIREMENT_ID,
        "work_item_id": WORK_ITEM_ID,
        "workspace_id": WORKSPACE_ID,
        "repository_id": REPOSITORY_ID,
        "branch_binding_id": BRANCH_BINDING_ID,
        "branch_name": BRANCH_NAME,
        "active": True,
        "fenced": False,
    }
    values.update(overrides)
    return AgentExecutionBindingSnapshot.model_validate(values)


def test_agent_push_spec_and_binding_accept_exact_immutable_coordinates() -> None:
    spec = _spec()
    binding = _binding()

    assert spec.target_commit_sha == TARGET_COMMIT
    assert spec.content_digest == CONTENT_DIGEST
    assert binding.execution_binding_digest == BINDING_DIGEST
    assert binding.matches(spec) is True
    assert binding.model_config["frozen"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_generation", 0),
        ("expected_remote_head_sha", "not-a-commit"),
        ("target_commit_sha", "A" * 40),
        ("content_digest", "sha256:short"),
        ("branch_name", " "),
        ("artifact_refs", tuple(f"artifact://{index}" for index in range(33))),
        ("artifact_refs", ("x" * 257,)),
    ],
)
def test_agent_push_spec_rejects_unbound_or_oversized_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        _spec(**{field: value})


def test_execution_binding_mismatch_and_fence_are_not_valid_matches() -> None:
    assert _binding(repository_id="another-repository").matches(_spec()) is False
    assert _binding(active=False).matches(_spec()) is False
    assert _binding(fenced=True).matches(_spec()) is False


@pytest.mark.parametrize(
    "expires_at",
    [
        NOW,
        NOW - timedelta(microseconds=1),
        NOW + timedelta(minutes=5, microseconds=1),
    ],
)
def test_agent_push_expiry_must_be_positive_and_within_absolute_ceiling(
    expires_at: datetime,
) -> None:
    with pytest.raises(InvalidAgentPushGrant):
        validate_agent_push_expiry(
            issued_at=NOW,
            expires_at=expires_at,
            max_ttl=timedelta(minutes=5),
        )


def test_agent_push_expiry_accepts_policy_bounded_short_ttl() -> None:
    validate_agent_push_expiry(
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        max_ttl=timedelta(seconds=60),
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AgentPushState.AUTHORIZED, AgentPushState.IN_FLIGHT),
        (AgentPushState.AUTHORIZED, AgentPushState.BLOCKED),
        (AgentPushState.IN_FLIGHT, AgentPushState.UNKNOWN),
        (AgentPushState.IN_FLIGHT, AgentPushState.SUCCEEDED),
        (AgentPushState.UNKNOWN, AgentPushState.RECONCILIATION),
        (AgentPushState.RECONCILIATION, AgentPushState.UNKNOWN),
        (AgentPushState.RECONCILIATION, AgentPushState.SUCCEEDED),
        (AgentPushState.RECONCILIATION, AgentPushState.BLOCKED),
        (AgentPushState.RECONCILIATION, AgentPushState.FENCED),
    ],
)
def test_agent_push_transition_allows_only_explicit_effect_progression(
    current: AgentPushState,
    target: AgentPushState,
) -> None:
    assert transition_agent_push(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AgentPushState.AUTHORIZED, AgentPushState.SUCCEEDED),
        (AgentPushState.UNKNOWN, AgentPushState.IN_FLIGHT),
        (AgentPushState.SUCCEEDED, AgentPushState.UNKNOWN),
        (AgentPushState.BLOCKED, AgentPushState.AUTHORIZED),
        (AgentPushState.FENCED, AgentPushState.SUCCEEDED),
    ],
)
def test_agent_push_transition_rejects_shortcuts_and_terminal_reopening(
    current: AgentPushState,
    target: AgentPushState,
) -> None:
    with pytest.raises(InvalidAgentPushTransition):
        transition_agent_push(current, target)


def test_fingerprints_are_deterministic_and_change_with_bound_content() -> None:
    original = agent_push_request_fingerprint(_spec())

    assert original == agent_push_request_fingerprint(_spec())
    assert original.startswith("sha256:")
    assert original != agent_push_request_fingerprint(_spec(target_commit_sha="e" * 40))


def test_grant_digest_is_one_way_and_errors_never_echo_raw_value() -> None:
    raw = "v10-raw-grant-sentinel-never-persist"
    digest = digest_agent_push_grant(raw)

    assert digest.startswith("sha256:")
    assert raw not in digest
    with pytest.raises(InvalidAgentPushGrant) as captured:
        digest_agent_push_grant(" ")
    assert raw not in str(captured.value)
    assert str(captured.value) == "Agent push grant is invalid"


def test_delivery_facts_are_versioned_mutually_exclusive_and_secret_free() -> None:
    base = {
        "attempt_id": ATTEMPT_ID,
        "attempt_generation": 3,
        "requirement_id": REQUIREMENT_ID,
        "work_item_id": WORK_ITEM_ID,
        "workspace_id": WORKSPACE_ID,
        "repository_id": REPOSITORY_ID,
        "branch_binding_id": BRANCH_BINDING_ID,
        "branch_name": BRANCH_NAME,
        "target_commit_sha": TARGET_COMMIT,
        "content_digest": CONTENT_DIGEST,
        "artifact_refs": ("artifact://patch/301",),
        "executor_type": "AGENT",
        "correlation_id": "correlation-301",
        "observed_at": NOW,
    }
    confirmed = AgentDeliveryFact(
        topic=AgentDeliveryFactTopic.CONFIRMED,
        payload=AgentDeliveryFactPayload.model_validate(base),
    )
    fenced = AgentDeliveryFact(
        topic=AgentDeliveryFactTopic.FENCED,
        payload=AgentDeliveryFactPayload.model_validate(
            {**base, "reason_code": "PUSH_OBSERVED_AFTER_FENCE"}
        ),
    )

    assert confirmed.payload.reason_code is None
    assert fenced.payload.reason_code == "PUSH_OBSERVED_AFTER_FENCE"
    assert confirmed.model_dump(mode="json")["topic"].endswith("confirmed.v1")
    serialized = str((confirmed.model_dump(mode="json"), fenced.model_dump(mode="json"))).lower()
    assert all(word not in serialized for word in ("grant", "fencing", "credential", "secret"))

    with pytest.raises(ValidationError):
        AgentDeliveryFact(
            topic=AgentDeliveryFactTopic.CONFIRMED,
            payload=fenced.payload,
        )
    with pytest.raises(ValidationError):
        AgentDeliveryFact(
            topic=AgentDeliveryFactTopic.FENCED,
            payload=confirmed.payload,
        )
