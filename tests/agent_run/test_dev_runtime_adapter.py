import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from pydantic import SecretStr

from control_plane.app.modules.agent_run import (
    DenialCode,
    EvidenceKind,
    EvidenceRef,
    MaterializationGuard,
    MaterializationHandle,
)
from control_plane.app.modules.agent_run.adapters.dev_runtime import (
    RestrictedDevSandboxAdapter,
)
from control_plane.app.modules.agent_run.adapters.sqlalchemy_repository import (
    SqlAlchemySandboxRepository,
    fencing_token_digest,
)
from control_plane.app.modules.agent_run.domain.policy import SandboxPolicyViolation
from control_plane.app.modules.agent_run.ports.repository import AdmissionPolicySnapshot
from control_plane.app.modules.agent_run.ports.runtime import RuntimeMaterializationRequest
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.factories import binding_projection


def _handle() -> MaterializationHandle:
    return MaterializationHandle(
        materialization_id="materialization-1",
        environment_id="environment-1",
        execution_id="execution-1",
        lease_id="lease-1",
        generation=1,
        fencing_token=SecretStr("local-test-fence-token"),
        revision=1,
        deadline_at=datetime.now(UTC) + timedelta(minutes=30),
    )


def _request() -> RuntimeMaterializationRequest:
    return RuntimeMaterializationRequest(
        operation_id="10000000-0000-0000-0000-000000000001",
        handle=_handle(),
        binding=binding_projection(),
    )


def test_restricted_dev_adapter_returns_matching_lab_only_readiness(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository-1"
    repository.mkdir()
    (repository / "app.py").write_text("print('repository data')\n", encoding="utf-8")
    adapter = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
    )

    readiness = adapter.provision(_request())

    assert readiness.materialization_id == "materialization-1"
    assert readiness.binding_digest == _request().binding.binding_digest
    assert readiness.generation == 1
    assert readiness.protocol_version == "1"
    assert readiness.lab_only is True
    assert readiness.isolation_evidence_refs == ()
    assert "repository data" not in repr(adapter.events)
    assert "local-test-fence-token" not in repr(adapter.events)


def test_runtime_observation_distinguishes_confirmed_absence_from_presence(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository-1"
    repository.mkdir()
    adapter = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
    )

    absent = adapter.observe("missing-materialization")
    adapter.provision(_request())
    present = adapter.observe("materialization-1")

    assert absent.presence.value == "ABSENT"
    assert present.presence.value == "PRESENT"
    assert present.materialization_id == "materialization-1"
    assert present.generation == 1


class _PausedPreviewAdapter(RestrictedDevSandboxAdapter):
    def __init__(self, root: Path) -> None:
        super().__init__(repository_root=root, repositories={"repository-1": root})
        self.publishing = Event()
        self.release = Event()

    def _record(self, action: str, guard: MaterializationGuard) -> None:
        if action == "publish_preview":
            self.publishing.set()
            assert self.release.wait(timeout=10)
        super()._record(action, guard)


def test_in_memory_dev_preview_and_fence_are_serialized(tmp_path: Path) -> None:
    runtime = _PausedPreviewAdapter(tmp_path)
    request = _request()
    request = request.model_copy(
        update={
            "binding": request.binding.model_copy(
                update={
                    "boundaries": request.binding.boundaries.model_copy(
                        update={"preview_enabled": True}
                    ),
                }
            )
        }
    )
    runtime.provision(request)
    guard = MaterializationGuard(
        materialization_id=request.handle.materialization_id,
        lease_id=request.handle.lease_id,
        generation=request.handle.generation,
        fencing_token=request.handle.fencing_token,
        expected_revision=1,
    )
    runtime.persist_evidence(guard, ())
    metadata = EvidenceRef(
        kind=EvidenceKind.PREVIEW_METADATA,
        artifact_id="preview-meta",
        version="1",
        sha256="sha256:" + "a" * 64,
        classification="INTERNAL",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        preview = pool.submit(
            runtime.publish_preview,
            "preview-operation-1",
            guard,
            metadata,
            request.handle.deadline_at,
        )
        assert runtime.publishing.wait(timeout=10)
        fenced = pool.submit(runtime.fence, guard)
        try:
            # Fence may start, but it cannot linearize inside the paused publication.
            assert not fenced.done()
        finally:
            runtime.release.set()
        preview.result(timeout=10)
        fenced.result(timeout=10)
    actions = [event.action for event in runtime.events]
    assert actions.index("publish_preview") < actions.index("fence")
    assert runtime.observe(guard.materialization_id).preview_access_active is False


def test_restricted_runtime_replays_stable_provision_after_adapter_restart(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository-1"
    repository.mkdir()
    binding = binding_projection(environment_id="10000000-0000-0000-0000-000000000901")
    token = "local-test-fence-token"
    with isolated_agent_run_database.runtime.begin() as db:
        reservation = SqlAlchemySandboxRepository(db).reserve_materialization(
            binding=binding,
            policy=AdmissionPolicySnapshot(
                policy_version="sandbox-policy-v1",
                enabled=True,
                active_attempt_limit=1,
                maximum_units=1,
                lease_ttl_seconds=1800,
            ),
            materialization_id=str(uuid4()),
            lease_id=str(uuid4()),
            generation_id=str(uuid4()),
            fencing_token_digest=fencing_token_digest(token),
            now=datetime.now(UTC),
        )
    handle = MaterializationHandle(
        materialization_id=reservation.materialization_id,
        environment_id=reservation.environment_id,
        execution_id=reservation.execution_id,
        lease_id=reservation.lease_id,
        generation=reservation.generation,
        fencing_token=SecretStr(token),
        revision=reservation.revision,
        deadline_at=reservation.deadline_at,
    )
    request = RuntimeMaterializationRequest(
        operation_id=str(uuid4()),
        handle=handle,
        binding=binding,
    )
    first_adapter = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
        state_engine=isolated_agent_run_database.runtime,
    )

    first = first_adapter.provision(request)

    restarted_adapter = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
        state_engine=isolated_agent_run_database.runtime,
    )
    replay = restarted_adapter.provision(request)
    observation = restarted_adapter.observe(request.handle.materialization_id)

    assert replay == first
    assert restarted_adapter.events == ()
    assert observation.presence.value == "PRESENT"
    assert observation.generation == request.handle.generation


def test_repository_control_directory_and_hooks_are_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository-1"
    hook = repository / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True)
    hook.write_text("malicious", encoding="utf-8")
    adapter = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
    )

    with pytest.raises(SandboxPolicyViolation) as caught:
        adapter.provision(_request())

    assert caught.value.code is DenialCode.RUNTIME_BOUNDARY_VIOLATION
    assert caught.value.failure_dimension == "repository_control_data"


def test_repository_symlink_escape_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository-1"
    repository.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = repository / "escape.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        os.link(outside, link)
    adapter = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
    )

    with pytest.raises(SandboxPolicyViolation) as caught:
        adapter.provision(_request())

    assert caught.value.code is DenialCode.RUNTIME_BOUNDARY_VIOLATION
    assert caught.value.failure_dimension == "repository_link"


def test_repository_mapping_cannot_escape_configured_root(tmp_path: Path) -> None:
    repository_root = tmp_path / "allowed"
    repository_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    adapter = RestrictedDevSandboxAdapter(
        repository_root=repository_root,
        repositories={"repository-1": outside},
    )

    with pytest.raises(SandboxPolicyViolation) as caught:
        adapter.provision(_request())

    assert caught.value.code is DenialCode.RUNTIME_BOUNDARY_VIOLATION
    assert caught.value.failure_dimension == "repository_root"
