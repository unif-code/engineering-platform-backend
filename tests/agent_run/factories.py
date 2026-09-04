from datetime import UTC, datetime, timedelta

from control_plane.app.modules.agent_run import (
    BoundaryManifest,
    ExecutionBindingProjection,
    ExecutionKind,
    ExecutionRef,
    RepositoryCheckout,
    ResourceProfileRef,
    RunnerManifestRef,
    SandboxEnvironmentRef,
    VersionedProfileRef,
)


def binding_projection(
    *,
    execution_id: str = "execution-1",
    environment_id: str = "environment-1",
    repository_id: str = "repository-1",
    deadline_at: datetime | None = None,
) -> ExecutionBindingProjection:
    return ExecutionBindingProjection(
        execution=ExecutionRef(
            execution_id=execution_id,
            kind=ExecutionKind.SINGLE_REPOSITORY_FIX,
        ),
        environment=SandboxEnvironmentRef(
            environment_id=environment_id,
            workspace_id="workspace-1",
            requirement_id="requirement-1",
        ),
        binding_digest="sha256:" + "2" * 64,
        deadline_at=deadline_at or datetime.now(UTC) + timedelta(minutes=30),
        runtime_profile=VersionedProfileRef(
            id="runtime-standard-v1",
            version="1",
            digest="sha256:" + "1" * 64,
        ),
        resource_profile=ResourceProfileRef(
            id="standard-v1",
            version="1",
            digest="sha256:" + "3" * 64,
            unit_weight=1,
        ),
        runner_manifest=RunnerManifestRef(
            image_digest="sha256:" + "4" * 64,
            protocol_version="1",
            runtime_engine_id="pydantic-ai",
            adapter_version="1",
            bundle_digest="sha256:" + "5" * 64,
        ),
        boundaries=BoundaryManifest(
            allowed_tool_ids=("code.read", "code.write", "test.run"),
            allowed_network_target_refs=("gateway:model", "gateway:artifact"),
            secret_lease_ref="secret-lease:model-gateway:lease-1",
            repository_checkout=RepositoryCheckout(
                repository_id=repository_id,
                branch_name="fix/task-1",
                base_commit_sha="a" * 40,
            ),
            tool_policy=VersionedProfileRef(
                id="tools-fix-v1",
                version="1",
                digest="sha256:" + "6" * 64,
            ),
            network_policy=VersionedProfileRef(
                id="network-default-deny-v1",
                version="1",
                digest="sha256:" + "7" * 64,
            ),
            secret_scope=VersionedProfileRef(
                id="model-gateway-only-v1",
                version="1",
                digest="sha256:" + "8" * 64,
            ),
        ),
        policy_versions=(
            VersionedProfileRef(
                id="agent.sandbox.active_attempt_limit",
                version="1",
                digest="sha256:" + "9" * 64,
            ),
        ),
    )
