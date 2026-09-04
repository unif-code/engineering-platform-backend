from datetime import UTC, datetime, timedelta
from typing import get_type_hints

import pytest
from pydantic import SecretStr, ValidationError

import control_plane.app.modules.agent_run as agent_run

EXPECTED_SANDBOX_METHODS = {
    "provision_materialization",
    "get_materialization_status",
    "publish_preview",
    "checkpoint_and_release",
    "handoff_to_child",
    "finalize_execution",
    "cancel_execution",
    "reconcile_lease",
}


def _binding() -> agent_run.ExecutionBindingProjection:
    profile = agent_run.VersionedProfileRef(
        id="runtime-standard-v1",
        version="1",
        digest="sha256:" + "1" * 64,
    )
    return agent_run.ExecutionBindingProjection(
        execution=agent_run.ExecutionRef(
            execution_id="execution-1",
            kind=agent_run.ExecutionKind.SINGLE_REPOSITORY_FIX,
        ),
        environment=agent_run.SandboxEnvironmentRef(
            environment_id="environment-1",
            workspace_id="workspace-1",
            requirement_id="requirement-1",
        ),
        binding_digest="sha256:" + "2" * 64,
        deadline_at=datetime.now(UTC) + timedelta(minutes=30),
        runtime_profile=profile,
        resource_profile=agent_run.ResourceProfileRef(
            id="standard-v1",
            version="1",
            digest="sha256:" + "3" * 64,
            unit_weight=1,
        ),
        runner_manifest=agent_run.RunnerManifestRef(
            image_digest="sha256:" + "4" * 64,
            protocol_version="1",
            runtime_engine_id="pydantic-ai",
            adapter_version="1",
            bundle_digest="sha256:" + "5" * 64,
        ),
        boundaries=agent_run.BoundaryManifest(
            allowed_tool_ids=("code.read", "code.write", "test.run"),
            allowed_network_target_refs=("gateway:model", "gateway:artifact"),
            secret_lease_ref="secret-lease:model-gateway:lease-1",
            repository_checkout=agent_run.RepositoryCheckout(
                repository_id="repository-1",
                branch_name="fix/task-1",
                base_commit_sha="a" * 40,
            ),
            tool_policy=agent_run.VersionedProfileRef(
                id="tools-fix-v1",
                version="1",
                digest="sha256:" + "6" * 64,
            ),
            network_policy=agent_run.VersionedProfileRef(
                id="network-default-deny-v1",
                version="1",
                digest="sha256:" + "7" * 64,
            ),
            secret_scope=agent_run.VersionedProfileRef(
                id="model-gateway-only-v1",
                version="1",
                digest="sha256:" + "8" * 64,
            ),
        ),
        policy_versions=(
            agent_run.VersionedProfileRef(
                id="agent.sandbox.active_attempt_limit",
                version="1",
                digest="sha256:" + "9" * 64,
            ),
        ),
    )


def test_package_root_exposes_the_stable_eight_method_sandbox_port() -> None:
    methods = {
        name
        for name, value in vars(agent_run.SandboxPort).items()
        if callable(value) and not name.startswith("_")
    }

    assert methods == EXPECTED_SANDBOX_METHODS


def test_contract_models_are_frozen_versioned_and_reject_extra_fields() -> None:
    binding = _binding()

    assert binding.schema_version == 1
    assert binding.boundaries.repository_checkout.access_mode.value == "READ_WRITE_WORKTREE"
    with pytest.raises(ValidationError):
        binding.execution.execution_id = "changed"
    with pytest.raises(ValidationError):
        agent_run.ExecutionRef.model_validate(
            {
                "execution_id": "execution-1",
                "kind": "SINGLE_REPOSITORY_FIX",
                "provider": "forbidden",
            }
        )


def test_fencing_token_is_secret_in_domain_representations() -> None:
    handle = agent_run.MaterializationHandle(
        materialization_id="materialization-1",
        environment_id="environment-1",
        execution_id="execution-1",
        lease_id="lease-1",
        generation=1,
        fencing_token=SecretStr("local-test-fence-token"),
        revision=1,
        deadline_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    assert "local-test-fence-token" not in repr(handle)
    assert handle.fencing_token.get_secret_value() == "local-test-fence-token"


def test_public_contract_annotations_contain_no_physical_or_provider_types() -> None:
    forbidden = {
        "kubernetes",
        "kata",
        "pod",
        "node",
        "runtimeclass",
        "provider",
        "region",
        "sku",
        "credential",
    }
    exported_annotations = " ".join(
        str(get_type_hints(getattr(agent_run, name)))
        for name in agent_run.__all__
        if isinstance(getattr(agent_run, name), type)
    ).lower()

    assert not any(term in exported_annotations for term in forbidden)
