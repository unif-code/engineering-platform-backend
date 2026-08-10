from control_plane.app.modules.audit import AuditEnvelope, record


class FakeRepository:
    def __init__(self) -> None:
        self.appended: list[AuditEnvelope] = []

    def append(self, envelope: AuditEnvelope) -> None:
        self.appended.append(envelope)


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
