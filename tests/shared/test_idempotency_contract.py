from datetime import UTC, datetime
from typing import Any

from control_plane.app.shared.idempotency import (
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)


class MemoryRepository:
    def __init__(self) -> None:
        self.row: dict[str, Any] | None = None

    def claim_idempotency(self, **values: Any) -> bool:
        if self.row is not None:
            return False
        self.row = {
            **values,
            "state": "IN_PROGRESS",
            "http_status": None,
            "result_metadata": None,
            "sealed_response": None,
        }
        return True

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        del actor, operation, idempotency_key, for_update
        return self.row

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool:
        assert self.row is not None and self.row["id"] == record_id
        self.row.update(
            state="COMPLETED",
            http_status=http_status,
            result_metadata=result_metadata,
            sealed_response=sealed_response,
            updated_at=now,
        )
        return True


def test_shared_fingerprint_preserves_identity_task6_compatibility() -> None:
    fingerprint = canonical_request_fingerprint(
        operation="org_set_superior",
        method="PUT",
        path="/api/v1/admin/accounts/a/superior",
        body={"reason": "legacy"},
        idempotency_sealing_key=b"s" * 32,
    )

    assert fingerprint == "1848579d13a6eba4a2535989871370f015617f3b1e25e00c23958717960985df"


def test_module_neutral_idempotency_helper_executes_and_replays_exact_response() -> None:
    repository = MemoryRepository()
    calls: list[str] = []
    response = IdempotentResponse(
        status_code=204,
        body={},
        headers={"ETag": '"v1"'},
    )

    def first_command() -> IdempotentResponse:
        calls.append("called")
        return response

    def duplicate_command() -> IdempotentResponse:
        calls.append("duplicate")
        return response

    first = execute_idempotent(
        repository,
        actor="00000999",
        operation="example_command",
        key="shared-key-0001",
        fingerprint="fingerprint",
        command=first_command,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        new_id=lambda: "00000000-0000-0000-0000-000000009999",
        idempotency_sealing_key=b"s" * 32,
    )
    second = execute_idempotent(
        repository,
        actor="00000999",
        operation="example_command",
        key="shared-key-0001",
        fingerprint="fingerprint",
        command=duplicate_command,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        new_id=lambda: "00000000-0000-0000-0000-000000009999",
        idempotency_sealing_key=b"s" * 32,
    )

    assert first.response == response
    assert first.replayed is False
    assert second.response == response
    assert second.replayed is True
    assert calls == ["called"]
