from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import Connection, create_engine

from control_plane.app.modules.audit import TransactionalAuditAppender
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
    ) -> None:
        self.messages = messages
        self.events = events
        self.acknowledgement_failures = acknowledgement_failures
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
        assert message_id == MESSAGE_ID
        self.events.append("ack")
        if self.acknowledgement_failures:
            self.acknowledgement_failures -= 1
            raise RuntimeError("provider/raw payload must not escape")

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        self.events.append("release")
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


def _message(*, payload_hash: str = "sha256:delivery-601") -> DeliveryRequestEnvelope:
    return DeliveryRequestEnvelope(
        message_id=MESSAGE_ID,
        topic="requirement.integration-merge-request.requested",
        payload_hash=payload_hash,
        requirement_id="40000000-0000-0000-0000-000000000601",
        requirement_revision=3,
        work_item_id="50000000-0000-0000-0000-000000000601",
        work_item_revision=5,
        repository_id="gitlab-project-601",
        actor_id="employee-1",
        kind=DeliveryRequestKind.CREATE_MR,
        integration_merge_request_binding_id=None,
        attempts=1,
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


def test_relay_releases_payload_hash_conflict_without_acknowledging() -> None:
    events: list[str] = []
    requirement_delivery = FakeRequirementDelivery((_message(),), events)
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
    assert requirement_delivery.releases == [(MESSAGE_ID, "DELIVERY_REQUEST_CONFLICT", NOW)]
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
