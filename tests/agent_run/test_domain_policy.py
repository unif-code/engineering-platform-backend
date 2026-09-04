from datetime import UTC, datetime, timedelta

import pytest

from control_plane.app.modules.agent_run import DenialCode
from control_plane.app.modules.agent_run.domain.policy import (
    SandboxPolicyViolation,
    validate_binding_for_provision,
)
from tests.agent_run.factories import binding_projection


def _violation(binding: object) -> SandboxPolicyViolation:
    with pytest.raises(SandboxPolicyViolation) as caught:
        validate_binding_for_provision(
            binding,  # type: ignore[arg-type]
            now=datetime.now(UTC),
        )
    return caught.value


def test_valid_single_repository_fix_binding_is_accepted() -> None:
    binding = binding_projection()

    validate_binding_for_provision(binding, now=datetime.now(UTC))


def test_expired_binding_fails_closed() -> None:
    error = _violation(binding_projection(deadline_at=datetime.now(UTC) - timedelta(seconds=1)))

    assert error.code is DenialCode.RUNTIME_BINDING_INVALID
    assert error.failure_dimension == "deadline"


@pytest.mark.parametrize(
    ("field", "value", "code", "dimension"),
    [
        (
            "tools",
            ("code.read", "code.write", "shell.unrestricted"),
            DenialCode.RUNTIME_CAPABILITY_DENIED,
            "tool",
        ),
        (
            "network",
            ("gateway:model", "https://model-provider.example"),
            DenialCode.RUNTIME_BOUNDARY_VIOLATION,
            "network",
        ),
        (
            "secret",
            "secret-lease:model-provider:raw-key",
            DenialCode.RUNTIME_BOUNDARY_VIOLATION,
            "secret",
        ),
    ],
)
def test_unbound_tool_network_and_secret_authority_are_denied(
    field: str,
    value: tuple[str, ...] | str,
    code: DenialCode,
    dimension: str,
) -> None:
    binding = binding_projection()
    boundaries = binding.boundaries
    if field == "tools":
        boundaries = boundaries.model_copy(update={"allowed_tool_ids": value})
    elif field == "network":
        boundaries = boundaries.model_copy(update={"allowed_network_target_refs": value})
    else:
        boundaries = boundaries.model_copy(update={"secret_lease_ref": value})

    error = _violation(binding.model_copy(update={"boundaries": boundaries}))

    assert error.code is code
    assert error.failure_dimension == dimension


def test_unknown_runner_protocol_and_oversized_profile_are_denied() -> None:
    binding = binding_projection()
    protocol_error = _violation(
        binding.model_copy(
            update={
                "runner_manifest": binding.runner_manifest.model_copy(
                    update={"protocol_version": "999"}
                )
            }
        )
    )
    resource_error = _violation(
        binding.model_copy(
            update={
                "resource_profile": binding.resource_profile.model_copy(update={"unit_weight": 2})
            }
        )
    )

    assert protocol_error.code is DenialCode.RUNTIME_BINDING_INVALID
    assert protocol_error.failure_dimension == "runner_protocol"
    assert resource_error.code is DenialCode.RESOURCE_EXHAUSTED
    assert resource_error.failure_dimension == "resource_profile"
