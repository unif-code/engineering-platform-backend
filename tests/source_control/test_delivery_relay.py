import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlIntegrationRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.application import (
    SourceControlDependencies,
    relay_integration_delivery_requests,
)
from control_plane.app.modules.source_control.domain import (
    DeliveryRequestEnvelope,
    DeliveryRequestKind,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.ports import (
    RandomPort,
    RequirementDeliveryPort,
    SourceControlIntegrationRepositoryFactory,
    SourceControlRepositoryFactory,
)

NOW = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)
MESSAGE_ID = "30000000-0000-0000-0000-000000000601"
SECOND_MESSAGE_ID = "30000000-0000-0000-0000-000000000602"


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FakeRequirementDelivery:
    def __init__(
        self,
        messages: tuple[DeliveryRequestEnvelope, ...],
        events: list[str],
        *,
        acknowledgement_failures: int = 0,
        release_failures: int = 0,
        on_acknowledge: Callable[[str], None] | None = None,
        on_release: Callable[[str], None] | None = None,
    ) -> None:
        self.messages = messages
        self.events = events
        self.acknowledgement_failures = acknowledgement_failures
        self.release_failures = release_failures
        self.on_acknowledge = on_acknowledge
        self.on_release = on_release
        self.acked_message_ids: list[str] = []
        self.releases: list[tuple[str, str, datetime]] = []

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[DeliveryRequestEnvelope, ...]:
        assert limit == 10
        assert lease_until == NOW + timedelta(seconds=30)
        self.events.append("claim")
        return self.messages

    def acknowledge_request(self, message_id: str) -> None:
        self.events.append("ack")
        if self.on_acknowledge is not None:
            self.on_acknowledge(message_id)
        if self.acknowledgement_failures:
            self.acknowledgement_failures -= 1
            raise RuntimeError("provider/raw payload must not escape")
        self.acked_message_ids.append(message_id)

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        self.events.append("release")
        if self.on_release is not None:
            self.on_release(message_id)
        if self.release_failures:
            self.release_failures -= 1
            raise RuntimeError("test-only-sensitive-provider-release-payload")
        self.releases.append((message_id, error_code, retry_at))


class FakeDeliveryRepository:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

    def delivery_request(
        self,
        message_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, object] | None:
        del for_update
        return self.rows.get(message_id)

    def accept_delivery_request(self, **values: Any) -> dict[str, object] | None:
        message_id = str(values["message_id"])
        if message_id in self.rows:
            return None
        row = dict(values)
        row.update(
            {
                "state": "RECEIVED",
                "attempts": 0,
                "available_at": values["now"],
                "last_error_code": None,
                "received_at": values["now"],
                "updated_at": values["now"],
                "processed_at": None,
            }
        )
        self.rows[message_id] = row
        return row


def _message(
    *,
    message_id: str = MESSAGE_ID,
    payload_hash: str = "sha256:delivery-601",
    attempts: int = 1,
    repository_id: str = "gitlab-project-601",
    work_item_id: str = "50000000-0000-0000-0000-000000000601",
) -> DeliveryRequestEnvelope:
    return DeliveryRequestEnvelope(
        message_id=message_id,
        topic="requirement.integration-merge-request.requested",
        payload_hash=payload_hash,
        requirement_id="40000000-0000-0000-0000-000000000601",
        requirement_revision=3,
        work_item_id=work_item_id,
        work_item_revision=5,
        repository_id=repository_id,
        actor_id="employee-1",
        kind=DeliveryRequestKind.CREATE_MR,
        integration_merge_request_binding_id=None,
        attempts=attempts,
    )


def _dependencies(
    requirement_delivery: FakeRequirementDelivery,
    repository: FakeDeliveryRepository,
    events: list[str],
) -> SourceControlDependencies:
    engine = create_engine("sqlite://")

    def delivery_repository_factory(_db: Connection) -> FakeDeliveryRepository:
        events.append("accept")
        return repository

    return SourceControlDependencies(
        repository_factory=cast(SourceControlRepositoryFactory, lambda _db: object()),
        engine=engine,
        requirement=None,
        eligibility=None,
        audit=cast(TransactionalAuditAppender, object()),
        clock=FixedClock(),
        random=cast(RandomPort, object()),
        delivery_repository_factory=cast(
            SourceControlIntegrationRepositoryFactory,
            delivery_repository_factory,
        ),
        requirement_delivery=cast(RequirementDeliveryPort, requirement_delivery),
        gitlab_merge_requests=None,
    )


def _postgres_dependencies(
    engine: Engine,
    requirement_delivery: FakeRequirementDelivery,
    *,
    delivery_repository_factory: SourceControlIntegrationRepositoryFactory = (
        SqlAlchemySourceControlIntegrationRepository
    ),
) -> SourceControlDependencies:
    return SourceControlDependencies(
        repository_factory=cast(SourceControlRepositoryFactory, lambda _db: object()),
        engine=engine,
        requirement=None,
        eligibility=None,
        audit=cast(TransactionalAuditAppender, object()),
        clock=FixedClock(),
        random=cast(RandomPort, object()),
        delivery_repository_factory=delivery_repository_factory,
        requirement_delivery=cast(RequirementDeliveryPort, requirement_delivery),
        gitlab_merge_requests=None,
    )


def _register_repository(engine: Engine, repository_id: str = "gitlab-project-601") -> None:
    with engine.begin() as db:
        SqlAlchemySourceControlRepository(db).insert_workspace_repository(
            id=repository_id,
            workspace_id="20000000-0000-0000-0000-000000000601",
            provider="GITLAB",
            project_id="601",
            project_path="platform/backend",
            default_branch="main",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref=None,
            status="AUTHORIZED",
            revision=1,
            now=NOW,
        )


def _inbox_count(engine: Engine, message_id: str | None = None) -> int:
    statement = "SELECT count(*) FROM source_control.delivery_request_inbox"
    parameters: dict[str, object] = {}
    if message_id is not None:
        statement += " WHERE message_id=:message_id"
        parameters["message_id"] = message_id
    with engine.connect() as db:
        return cast(int, db.execute(text(statement), parameters).scalar_one())


class InsertThenFailDeliveryRepository:
    def __init__(self, db: Connection) -> None:
        self.repository = SqlAlchemySourceControlIntegrationRepository(db)

    def delivery_request(
        self,
        message_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        return self.repository.delivery_request(message_id, for_update=for_update)

    def accept_delivery_request(self, **values: Any) -> Any:
        inserted = self.repository.accept_delivery_request(**values)
        if inserted is not None:
            raise RuntimeError("test-only-sensitive-provider-accept-payload")
        return inserted


def test_relay_orders_requirement_claim_source_control_accept_then_ack() -> None:
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery((_message(),), events)
    repository = FakeDeliveryRepository()
    dependencies = _dependencies(requirement_delivery, repository, events)

    result = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert events == ["claim", "accept", "ack"]
    assert result.model_dump() == {"claimed": 1, "accepted": 1, "released": 0}
    assert tuple(repository.rows) == (MESSAGE_ID,)
    dependencies.engine.dispose()


def test_relay_replay_after_ack_failure_keeps_one_inbox() -> None:
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery(
        (_message(),),
        events,
        acknowledgement_failures=1,
    )
    repository = FakeDeliveryRepository()
    dependencies = _dependencies(requirement_delivery, repository, events)

    with pytest.raises(RequirementCallbackUnavailable) as raised:
        relay_integration_delivery_requests(limit=10, dependencies=dependencies)
    replay = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert "provider/raw payload" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert len(repository.rows) == 1
    assert replay.accepted == 1
    assert events == ["claim", "accept", "ack", "claim", "accept", "ack"]
    dependencies.engine.dispose()


@pytest.mark.parametrize(
    ("attempts", "expected_retry_at"),
    [
        (1, NOW + timedelta(minutes=1)),
        (5, NOW + timedelta(minutes=16)),
        (50, NOW + timedelta(hours=24)),
    ],
)
def test_relay_releases_payload_hash_conflict_with_bounded_backoff_without_acknowledging(
    attempts: int,
    expected_retry_at: datetime,
) -> None:
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery((_message(attempts=attempts),), events)
    repository = FakeDeliveryRepository()
    repository.accept_delivery_request(
        message_id=MESSAGE_ID,
        topic="requirement.integration-merge-request.requested",
        payload_hash="sha256:original",
        requirement_id="40000000-0000-0000-0000-000000000601",
        requirement_revision=3,
        work_item_id="50000000-0000-0000-0000-000000000601",
        work_item_revision=5,
        repository_id="gitlab-project-601",
        actor_id="employee-1",
        integration_merge_request_binding_id=None,
        now=NOW,
    )
    dependencies = _dependencies(requirement_delivery, repository, events)

    result = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert events == ["claim", "accept", "release"]
    assert result.model_dump() == {"claimed": 1, "accepted": 0, "released": 1}
    assert requirement_delivery.releases == [
        (MESSAGE_ID, "DELIVERY_REQUEST_CONFLICT", expected_retry_at)
    ]
    assert expected_retry_at > NOW
    assert repository.rows[MESSAGE_ID]["payload_hash"] == "sha256:original"
    dependencies.engine.dispose()


def test_relay_releases_local_accept_failure_with_safe_error_code() -> None:
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery((_message(),), events)
    repository = FakeDeliveryRepository()
    dependencies = _dependencies(requirement_delivery, repository, events)

    def unavailable_factory(_db: Connection) -> FakeDeliveryRepository:
        events.append("accept")
        raise RuntimeError("provider/raw payload must not escape")

    dependencies = replace(
        dependencies,
        delivery_repository_factory=cast(
            SourceControlIntegrationRepositoryFactory,
            unavailable_factory,
        ),
    )

    result = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert result.released == 1
    assert requirement_delivery.releases == [
        (MESSAGE_ID, "SOURCE_CONTROL_UNAVAILABLE", NOW + timedelta(minutes=5))
    ]
    assert events == ["claim", "accept", "release"]
    dependencies.engine.dispose()


@pytest.mark.parametrize("missing", ["requirement_delivery", "delivery_repository_factory"])
def test_relay_fails_closed_only_when_used_dependency_is_missing(missing: str) -> None:
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery((), events)
    repository = FakeDeliveryRepository()
    dependencies = _dependencies(requirement_delivery, repository, events)
    if missing == "requirement_delivery":
        dependencies = replace(dependencies, requirement_delivery=None)
    else:
        dependencies = replace(dependencies, delivery_repository_factory=None)

    with pytest.raises(SourceControlDependencyUnavailable) as raised:
        relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert "provider" not in str(raised.value).lower()
    assert events == []
    dependencies.engine.dispose()


def test_relay_does_not_require_gitlab_merge_request_dependency() -> None:
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery((), events)
    repository = FakeDeliveryRepository()
    dependencies = _dependencies(requirement_delivery, repository, events)

    result = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert result.claimed == 0
    assert dependencies.gitlab_merge_requests is None
    dependencies.engine.dispose()


def test_postgres_accept_exception_rolls_back_before_requirement_release(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _register_repository(isolated_source_control_rw_engine)
    count_seen_at_release: list[int] = []
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery(
        (_message(),),
        events,
        on_release=lambda message_id: count_seen_at_release.append(
            _inbox_count(isolated_source_control_rw_engine, message_id)
        ),
    )
    dependencies = _postgres_dependencies(
        isolated_source_control_rw_engine,
        requirement_delivery,
        delivery_repository_factory=cast(
            SourceControlIntegrationRepositoryFactory,
            InsertThenFailDeliveryRepository,
        ),
    )

    result = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert result.model_dump() == {"claimed": 1, "accepted": 0, "released": 1}
    assert count_seen_at_release == [0]
    assert _inbox_count(isolated_source_control_rw_engine) == 0
    assert requirement_delivery.releases == [
        (MESSAGE_ID, "SOURCE_CONTROL_UNAVAILABLE", NOW + timedelta(minutes=5))
    ]


def test_postgres_ack_runs_after_commit_and_replay_keeps_exactly_one_inbox(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _register_repository(isolated_source_control_rw_engine)
    count_seen_at_ack: list[int] = []
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery(
        (_message(),),
        events,
        acknowledgement_failures=1,
        on_acknowledge=lambda message_id: count_seen_at_ack.append(
            _inbox_count(isolated_source_control_rw_engine, message_id)
        ),
    )
    dependencies = _postgres_dependencies(
        isolated_source_control_rw_engine,
        requirement_delivery,
    )

    with pytest.raises(RequirementCallbackUnavailable):
        relay_integration_delivery_requests(limit=10, dependencies=dependencies)
    replay = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert count_seen_at_ack == [1, 1]
    assert _inbox_count(isolated_source_control_rw_engine, MESSAGE_ID) == 1
    assert replay.accepted == 1
    assert requirement_delivery.acked_message_ids == [MESSAGE_ID]


def test_postgres_same_id_different_hash_releases_real_repository_conflict(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _register_repository(isolated_source_control_rw_engine)
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery((_message(),), events)
    dependencies = _postgres_dependencies(
        isolated_source_control_rw_engine,
        requirement_delivery,
    )
    relay_integration_delivery_requests(limit=10, dependencies=dependencies)
    requirement_delivery.messages = (
        _message(payload_hash="sha256:changed-delivery-601", attempts=2),
    )

    conflict = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert conflict.model_dump() == {"claimed": 1, "accepted": 0, "released": 1}
    assert requirement_delivery.releases == [
        (MESSAGE_ID, "DELIVERY_REQUEST_CONFLICT", NOW + timedelta(minutes=2))
    ]
    assert requirement_delivery.acked_message_ids == [MESSAGE_ID]
    assert _inbox_count(isolated_source_control_rw_engine, MESSAGE_ID) == 1


def test_postgres_release_failure_is_sanitized_after_real_conflict(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _register_repository(isolated_source_control_rw_engine)
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery((_message(),), events)
    dependencies = _postgres_dependencies(
        isolated_source_control_rw_engine,
        requirement_delivery,
    )
    relay_integration_delivery_requests(limit=10, dependencies=dependencies)
    requirement_delivery.messages = (
        _message(payload_hash="sha256:changed-delivery-601", attempts=2),
    )
    requirement_delivery.release_failures = 1

    with pytest.raises(RequirementCallbackUnavailable) as raised:
        relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    formatted = "".join(traceback.format_exception(raised.value))
    assert str(raised.value) == "Requirement release unavailable"
    assert raised.value.__cause__ is None
    assert "test-only-sensitive-provider-release-payload" not in formatted
    assert _inbox_count(isolated_source_control_rw_engine, MESSAGE_ID) == 1


def test_postgres_releasable_failure_does_not_block_later_batch_message(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _register_repository(isolated_source_control_rw_engine)
    events: list[str] = []
    missing_repository_message = _message(repository_id="gitlab-project-missing")
    valid_message = _message(
        message_id=SECOND_MESSAGE_ID,
        payload_hash="sha256:delivery-602",
        work_item_id="50000000-0000-0000-0000-000000000602",
    )
    requirement_delivery = FakeRequirementDelivery(
        (missing_repository_message, valid_message),
        events,
    )
    dependencies = _postgres_dependencies(
        isolated_source_control_rw_engine,
        requirement_delivery,
    )

    result = relay_integration_delivery_requests(limit=10, dependencies=dependencies)

    assert result.model_dump() == {"claimed": 2, "accepted": 1, "released": 1}
    assert requirement_delivery.releases == [
        (MESSAGE_ID, "SOURCE_CONTROL_UNAVAILABLE", NOW + timedelta(minutes=5))
    ]
    assert requirement_delivery.acked_message_ids == [SECOND_MESSAGE_ID]
    assert _inbox_count(isolated_source_control_rw_engine, MESSAGE_ID) == 0
    assert _inbox_count(isolated_source_control_rw_engine, SECOND_MESSAGE_ID) == 1
