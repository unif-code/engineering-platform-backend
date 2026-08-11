from typing import cast

from sqlalchemy import Connection

from control_plane.app.modules.audit import AuditEnvelope, record, record_in_transaction


class FakeRepository:
    def __init__(self) -> None:
        self.appended: list[AuditEnvelope] = []

    def append(self, envelope: AuditEnvelope) -> None:
        self.appended.append(envelope)


class FakeTransactionalAppender:
    def __init__(self) -> None:
        self.appended: list[tuple[Connection, AuditEnvelope]] = []

    def append_in_transaction(self, db: Connection, envelope: AuditEnvelope) -> None:
        self.appended.append((db, envelope))


def test_record_appends_envelope_with_generated_fields() -> None:
    repo = FakeRepository()
    envelope = AuditEnvelope(
        actor="00000000",
        actor_type="HUMAN",
        action="test.append",
        target_type="TEST",
        target_id="t-1",
        result="OK",
        correlation_id="corr-1",
    )
    record(envelope, repo)
    assert repo.appended == [envelope]
    assert envelope.id and envelope.schema_version == 1
    assert envelope.occurred_at.tzinfo is not None


def test_transactional_application_delegates_sql_to_the_injected_port() -> None:
    db = cast(Connection, object())
    appender = FakeTransactionalAppender()
    envelope = AuditEnvelope(
        actor="SYSTEM",
        actor_type="SYSTEM",
        action="test.transactional",
        target_type="TEST",
        target_id="t-2",
        result="OK",
        correlation_id="corr-2",
    )

    record_in_transaction(db, envelope, appender)

    assert appender.appended == [(db, envelope)]
