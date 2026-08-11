import threading
from collections.abc import Callable

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.identity.adapters.sqlalchemy import SqlAlchemyIdentityRepository
from control_plane.app.modules.identity.application.idempotency import (
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from tests.identity.task5_helpers import dependencies

pytestmark = pytest.mark.integration


def _fingerprint(body: dict[str, object]) -> str:
    return canonical_request_fingerprint(
        operation="auth_login",
        method="POST",
        path="/api/v1/auth/login",
        body=body,
    )


def test_completed_command_replays_sealed_result_without_reexecution(
    identity_rw_engine: Engine,
    clean_identity_db: None,
) -> None:
    calls = 0
    credential = "challenge-sensitive-value"

    def command() -> IdempotentResponse:
        nonlocal calls
        calls += 1
        return IdempotentResponse(
            status_code=200,
            body={"state": "TOTP_REQUIRED", "challengeToken": credential},
        )

    fingerprint = _fingerprint({"employeeNo": "00000001", "password": "secret"})
    with identity_rw_engine.begin() as db:
        first = execute_idempotent(
            SqlAlchemyIdentityRepository(db),
            actor="employee:00000001",
            operation="auth_login",
            key="replay-key-0001",
            fingerprint=fingerprint,
            command=command,
            dependencies=dependencies(),
        )
    with identity_rw_engine.begin() as db:
        second = execute_idempotent(
            SqlAlchemyIdentityRepository(db),
            actor="employee:00000001",
            operation="auth_login",
            key="replay-key-0001",
            fingerprint=fingerprint,
            command=command,
            dependencies=dependencies(),
        )
        stored = (
            db.execute(
                text(
                    "SELECT state, result_metadata, sealed_response "
                    "FROM identity.idempotency_record"
                )
            )
            .mappings()
            .one()
        )

    assert first.replayed is False
    assert second.replayed is True
    assert second.response == first.response
    assert calls == 1
    assert stored["state"] == "COMPLETED"
    assert stored["result_metadata"] == {"kind": "http-response", "schemaVersion": 1}
    assert credential.encode() not in stored["sealed_response"]
    assert b"secret" not in stored["sealed_response"]


def test_same_key_with_different_fingerprint_conflicts(
    identity_rw_engine: Engine,
    clean_identity_db: None,
) -> None:
    with identity_rw_engine.begin() as db:
        execute_idempotent(
            SqlAlchemyIdentityRepository(db),
            actor="employee:00000001",
            operation="auth_login",
            key="conflict-key-001",
            fingerprint=_fingerprint({"employeeNo": "00000001", "password": "first"}),
            command=lambda: IdempotentResponse(status_code=401, body={"title": "denied"}),
            dependencies=dependencies(),
        )

    with identity_rw_engine.begin() as db, pytest.raises(IdempotencyConflict):
        execute_idempotent(
            SqlAlchemyIdentityRepository(db),
            actor="employee:00000001",
            operation="auth_login",
            key="conflict-key-001",
            fingerprint=_fingerprint({"employeeNo": "00000001", "password": "second"}),
            command=lambda: pytest.fail("conflicting command must not execute"),
            dependencies=dependencies(),
        )


def test_tampered_replay_fails_closed_without_reexecution(
    identity_rw_engine: Engine,
    clean_identity_db: None,
) -> None:
    fingerprint = _fingerprint({"employeeNo": "00000001", "password": "secret"})
    with identity_rw_engine.begin() as db:
        execute_idempotent(
            SqlAlchemyIdentityRepository(db),
            actor="employee:00000001",
            operation="auth_login",
            key="tampered-key-01",
            fingerprint=fingerprint,
            command=lambda: IdempotentResponse(status_code=200, body={"state": "ok"}),
            dependencies=dependencies(),
        )
    with identity_rw_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.idempotency_record "
                "SET sealed_response=set_byte(sealed_response, 12, "
                "get_byte(sealed_response, 12) # 1)"
            )
        )

    with identity_rw_engine.begin() as db, pytest.raises(IdempotencyReplayUnavailable):
        execute_idempotent(
            SqlAlchemyIdentityRepository(db),
            actor="employee:00000001",
            operation="auth_login",
            key="tampered-key-01",
            fingerprint=fingerprint,
            command=lambda: pytest.fail("tampered replay must not execute"),
            dependencies=dependencies(),
        )


def test_authenticated_ciphertext_cannot_be_swapped_between_command_records(
    identity_rw_engine: Engine,
    clean_identity_db: None,
) -> None:
    first_fingerprint = _fingerprint({"employeeNo": "00000001", "password": "first"})
    second_fingerprint = _fingerprint({"employeeNo": "00000001", "password": "second"})
    with identity_rw_engine.begin() as db:
        first = SqlAlchemyIdentityRepository(db)
        execute_idempotent(
            first,
            actor="employee:00000001",
            operation="auth_login",
            key="swap-record-key1",
            fingerprint=first_fingerprint,
            command=lambda: IdempotentResponse(status_code=200, body={"winner": "first"}),
            dependencies=dependencies(),
        )
        execute_idempotent(
            first,
            actor="employee:00000001",
            operation="auth_login",
            key="swap-record-key2",
            fingerprint=second_fingerprint,
            command=lambda: IdempotentResponse(status_code=200, body={"winner": "second"}),
            dependencies=dependencies(),
        )
        db.execute(
            text(
                "UPDATE identity.idempotency_record target "
                "SET sealed_response=source.sealed_response "
                "FROM identity.idempotency_record source "
                "WHERE target.idempotency_key='swap-record-key1' "
                "AND source.idempotency_key='swap-record-key2'"
            )
        )

    with identity_rw_engine.begin() as db, pytest.raises(IdempotencyReplayUnavailable):
        execute_idempotent(
            SqlAlchemyIdentityRepository(db),
            actor="employee:00000001",
            operation="auth_login",
            key="swap-record-key1",
            fingerprint=first_fingerprint,
            command=lambda: pytest.fail("swapped replay must not execute"),
            dependencies=dependencies(),
        )


def test_two_connections_converge_and_execute_command_once(
    identity_rw_engine: Engine,
    clean_identity_db: None,
) -> None:
    barrier = threading.Barrier(2)
    responses: list[tuple[bool, str]] = []
    errors: list[BaseException] = []
    response_lock = threading.Lock()
    fingerprint = _fingerprint({"employeeNo": "00000001", "password": "secret"})

    def worker(command_factory: Callable[[], IdempotentResponse]) -> None:
        try:
            barrier.wait(timeout=5)
            with identity_rw_engine.begin() as db:
                execution = execute_idempotent(
                    SqlAlchemyIdentityRepository(db),
                    actor="employee:00000001",
                    operation="auth_login",
                    key="concurrent-key01",
                    fingerprint=fingerprint,
                    command=command_factory,
                    dependencies=dependencies(),
                )
            with response_lock:
                responses.append((execution.replayed, execution.response.body["winner"]))
        except BaseException as exc:
            with response_lock:
                errors.append(exc)

    def first_command() -> IdempotentResponse:
        return IdempotentResponse(status_code=200, body={"winner": "one"})

    def second_command() -> IdempotentResponse:
        return IdempotentResponse(status_code=200, body={"winner": "two"})

    threads = [
        threading.Thread(target=worker, args=(first_command,)),
        threading.Thread(target=worker, args=(second_command,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(responses) == 2
    assert sorted(replayed for replayed, _ in responses) == [False, True]
    assert len({winner for _, winner in responses}) == 1
