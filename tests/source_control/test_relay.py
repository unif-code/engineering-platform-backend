from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine

from control_plane.app.modules.source_control import (
    BindingRequestEnvelope,
    BindingRequestMessageConflict,
    RepositoryRemoved,
    RequirementCallbackUnavailable,
    SourceControlDependencies,
    accept_binding_request,
    register_workspace_repository,
    relay_binding_requests,
    remove_workspace_repository,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.ports import (
    BindingBlockedResult,
    BindingReadyResult,
    RequirementBindingContext,
)

NOW = datetime(2026, 8, 26, 4, 30, tzinfo=UTC)
REPOSITORY_ID = "gitlab-project-511"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000511"
MESSAGE_ID = "30000000-0000-0000-0000-000000000511"
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000511"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000511"


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedRandom:
    def __init__(self) -> None:
        self._next = 510

    def uuid4(self) -> UUID:
        self._next += 1
        return UUID(f"90000000-0000-0000-0000-{self._next:012d}")


class FakeAudit:
    def append_in_transaction(self, _db: object, _envelope: object) -> None:
        return None


class FakeRequirementPort:
    def __init__(self, message: BindingRequestEnvelope) -> None:
        self.message = message
        self.fail_next_ack = False
        self.acked_message_ids: list[str] = []
        self.released: list[tuple[str, str]] = []

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[BindingRequestEnvelope, ...]:
        del lease_until
        if self.message.message_id in self.acked_message_ids or limit == 0:
            return ()
        return (self.message,)

    def acknowledge_request(self, message_id: str) -> None:
        if self.fail_next_ack:
            self.fail_next_ack = False
            raise RequirementCallbackUnavailable("ack unavailable")
        self.acked_message_ids.append(message_id)

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        del retry_at
        self.released.append((message_id, error_code))

    def binding_context(self, work_item_id: str) -> RequirementBindingContext:
        raise AssertionError(f"unexpected binding context call: {work_item_id}")

    def record_ready(self, result: BindingReadyResult) -> None:
        raise AssertionError(f"unexpected ready callback: {result}")

    def record_blocked(self, result: BindingBlockedResult) -> None:
        raise AssertionError(f"unexpected blocked callback: {result}")


def _message(*, requirement_version: int = 1) -> BindingRequestEnvelope:
    return BindingRequestEnvelope(
        message_id=MESSAGE_ID,
        topic="requirement.repository-binding.requested",
        requirement_id=REQUIREMENT_ID,
        requirement_version=requirement_version,
        work_item_id=WORK_ITEM_ID,
        repository_id=REPOSITORY_ID,
        attempts=1,
    )


def _dependencies(engine: Engine, requirement: FakeRequirementPort) -> SourceControlDependencies:
    return SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=engine,
        requirement=requirement,
        eligibility=None,
        audit=FakeAudit(),
        clock=FixedClock(),
        random=FixedRandom(),
    )


def _register(engine: Engine, dependencies: SourceControlDependencies) -> None:
    with engine.begin() as db:
        register_workspace_repository(
            SqlAlchemySourceControlRepository(db),
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref=None,
            actor="SYSTEM",
            dependencies=dependencies,
        )


def test_relay_replays_after_requirement_ack_failure_without_duplicate_inbox(
    isolated_source_control_rw_engine: Engine,
) -> None:
    requirement = FakeRequirementPort(_message())
    dependencies = _dependencies(isolated_source_control_rw_engine, requirement)
    _register(isolated_source_control_rw_engine, dependencies)
    requirement.fail_next_ack = True

    with pytest.raises(RequirementCallbackUnavailable):
        relay_binding_requests(limit=10, dependencies=dependencies)
    with isolated_source_control_rw_engine.connect() as db:
        count_after_crash = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.binding_request_inbox"
        ).scalar_one()

    result = relay_binding_requests(limit=10, dependencies=dependencies)
    with isolated_source_control_rw_engine.connect() as db:
        count_after_replay = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.binding_request_inbox"
        ).scalar_one()

    assert count_after_crash == 1
    assert result.accepted == 1
    assert count_after_replay == 1
    assert requirement.acked_message_ids == [MESSAGE_ID]


def test_same_message_id_with_different_digest_conflicts(
    isolated_source_control_rw_engine: Engine,
) -> None:
    requirement = FakeRequirementPort(_message())
    dependencies = _dependencies(isolated_source_control_rw_engine, requirement)
    _register(isolated_source_control_rw_engine, dependencies)
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        first = accept_binding_request(repository, _message(), now=NOW)
        same = accept_binding_request(repository, _message(), now=NOW)
        with pytest.raises(BindingRequestMessageConflict):
            accept_binding_request(repository, _message(requirement_version=2), now=NOW)

    assert first.message_id == same.message_id


def test_removed_repository_blocks_new_requests_but_preserves_historical_inbox(
    isolated_source_control_rw_engine: Engine,
) -> None:
    requirement = FakeRequirementPort(_message())
    dependencies = _dependencies(isolated_source_control_rw_engine, requirement)
    _register(isolated_source_control_rw_engine, dependencies)
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        existing = accept_binding_request(repository, _message(), now=NOW)
        remove_workspace_repository(
            repository,
            repository_id=REPOSITORY_ID,
            expected_revision=1,
            actor="SYSTEM",
            dependencies=dependencies,
        )
        replay = accept_binding_request(repository, _message(), now=NOW)
        with pytest.raises(RepositoryRemoved):
            accept_binding_request(
                repository,
                _message().model_copy(
                    update={
                        "message_id": "30000000-0000-0000-0000-000000000512",
                        "work_item_id": "50000000-0000-0000-0000-000000000512",
                    }
                ),
                now=NOW,
            )

    assert existing.message_id == replay.message_id
