import hashlib
from datetime import datetime

from control_plane.app.modules.agent_run.domain.models import (
    DenialCode,
    ExecutionBindingProjection,
    RepositoryAccessMode,
    aware_now,
)

_REQUIRED_TOOLS = frozenset({"code.read", "code.write", "test.run"})
_ALLOWED_NETWORK_TARGETS = frozenset({"gateway:model", "gateway:artifact"})
_FORBIDDEN_SECRET_CLASSES = (
    "api-key",
    "credential",
    "gitlab",
    "provider",
    "registry",
    "source-control",
)


class SandboxPolicyViolation(RuntimeError):
    def __init__(self, code: DenialCode, failure_dimension: str) -> None:
        super().__init__(f"{code.value}:{failure_dimension}")
        self.code = code
        self.failure_dimension = failure_dimension


def fencing_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _deny(code: DenialCode, dimension: str) -> None:
    raise SandboxPolicyViolation(code, dimension)


def validate_binding_for_provision(
    binding: ExecutionBindingProjection,
    *,
    now: datetime,
) -> None:
    current = aware_now(now)
    if binding.deadline_at <= current:
        _deny(DenialCode.RUNTIME_BINDING_INVALID, "deadline")
    if binding.runtime_profile.id != "runtime-standard-v1":
        _deny(DenialCode.RUNTIME_BINDING_INVALID, "runtime_profile")
    if binding.resource_profile.id != "standard-v1" or binding.resource_profile.unit_weight != 1:
        _deny(DenialCode.RESOURCE_EXHAUSTED, "resource_profile")
    if binding.runner_manifest.protocol_version != "1":
        _deny(DenialCode.RUNTIME_BINDING_INVALID, "runner_protocol")
    if binding.runner_manifest.runtime_engine_id != "pydantic-ai":
        _deny(DenialCode.RUNTIME_BINDING_INVALID, "runtime_engine")

    boundaries = binding.boundaries
    tools = frozenset(boundaries.allowed_tool_ids)
    if tools != _REQUIRED_TOOLS:
        _deny(DenialCode.RUNTIME_CAPABILITY_DENIED, "tool")
    targets = frozenset(boundaries.allowed_network_target_refs)
    if not targets <= _ALLOWED_NETWORK_TARGETS or "gateway:model" not in targets:
        _deny(DenialCode.RUNTIME_BOUNDARY_VIOLATION, "network")
    secret_ref = boundaries.secret_lease_ref.lower()
    if not secret_ref.startswith("secret-lease:model-gateway:") or any(
        forbidden in secret_ref for forbidden in _FORBIDDEN_SECRET_CLASSES
    ):
        _deny(DenialCode.RUNTIME_BOUNDARY_VIOLATION, "secret")
    if boundaries.secret_scope.id != "model-gateway-only-v1":
        _deny(DenialCode.RUNTIME_BOUNDARY_VIOLATION, "secret_scope")
    if boundaries.network_policy.id != "network-default-deny-v1":
        _deny(DenialCode.RUNTIME_BOUNDARY_VIOLATION, "network_policy")
    if boundaries.tool_policy.id != "tools-fix-v1":
        _deny(DenialCode.RUNTIME_CAPABILITY_DENIED, "tool_policy")
    if boundaries.repository_checkout.access_mode is not RepositoryAccessMode.READ_WRITE_WORKTREE:
        _deny(DenialCode.RUNTIME_BOUNDARY_VIOLATION, "repository_access")
    if not any(
        policy.id == "agent.sandbox.active_attempt_limit" for policy in binding.policy_versions
    ):
        _deny(DenialCode.RUNTIME_BINDING_INVALID, "policy_snapshot")
