import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from typing import Any, cast
from uuid import uuid4

import pyotp
import pytest
from sqlalchemy import Connection, event, text
from sqlalchemy.exc import ProgrammingError

import control_plane.app.modules.identity as identity
from control_plane.app.modules.configuration.adapters.effective_policy import (
    IdentityEffectivePolicy,
)
from tests.identity.conftest import IsolatedIdentityDatabase
from tests.identity.task5_helpers import MutableClock, dependencies
from tests.identity.test_auth_flow import _initialize_account
from tests.identity.test_policy_reauthentication import binding

pytestmark = pytest.mark.integration

ReauthSetup = tuple[
    IsolatedIdentityDatabase,
    identity.IdentityDependencies,
    identity.IdentityPolicyReauthenticationRuntime,
    identity.PolicyReauthBinding,
    str,
    str,
    str,
]


@pytest.fixture
def ready(
    isolated_identity_database: IsolatedIdentityDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> ReauthSetup:
    database = isolated_identity_database
    deps = replace(dependencies(), policy=IdentityEffectivePolicy())
    secret, token = _initialize_account(database.runtime, deps, monkeypatch)
    with database.owner.begin() as db:
        actor = str(
            db.execute(
                text(
                    "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                    "WHERE employee_no='00000001' RETURNING id"
                )
            ).scalar_one()
        )
        session = str(
            db.execute(text("SELECT id FROM identity.session WHERE kind='FULL'")).scalar_one()
        )
    cast(MutableClock, deps.clock).value += timedelta(seconds=30)
    runtime = identity.IdentityPolicyReauthenticationRuntime(database.runtime, deps)
    return database, deps, runtime, binding(actor_id=actor), token, secret, session


def consume(ready: ReauthSetup, **changes: Any) -> identity.ConsumedReauthReceipt:
    _database, deps, runtime, value, token, secret, _session = ready
    assert runtime is not None, "independent reauth runtime is missing"
    values: dict[str, Any] = dict(
        raw_session=token,
        totp_code=pyotp.TOTP(secret).at(deps.clock.now()),
        binding=value,
        attempt_id=value.command_attempt_id,
    )
    values.update(changes)
    return runtime.verify_and_consume_policy_reauth(**values)


def test_consumption_commits_exact_fact_and_audit_and_can_be_finally_validated(
    ready: ReauthSetup,
) -> None:
    database, deps, runtime, value, token, _secret, session = ready
    receipt = consume(ready)
    assert receipt.binding == value
    assert receipt.session_reference == session
    assert receipt.account_version > 1
    assert receipt.consumed_at == deps.clock.now()
    assert receipt.expires_at == deps.clock.now() + timedelta(minutes=5)
    with database.owner.connect() as db:
        row = db.execute(text("SELECT * FROM identity.policy_reauth_consumption")).mappings().one()
        assert str(row["id"]) == receipt.receipt_id
        assert row["binding_hash"] == value.canonical_hash
        assert row["binding"] == json.loads(value.canonical_json())
        assert str(row["session_reference"]) == session
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.policy_reauth.consumed' AND result='SUCCESS'"
                )
            ).scalar_one()
            == 1
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM identity.auth_challenge "
                    "WHERE purpose='POLICY_PUBLISH' AND consumed_at IS NOT NULL"
                )
            ).scalar_one()
            == 1
        )
        assert token not in json.dumps(dict(row), default=str)
    runtime.validate_consumed_policy_reauth(raw_session=token, binding=value, receipt=receipt)


@pytest.mark.parametrize(
    "mutation", ["actor", "attempt", "token", "revoked", "bootstrap", "disabled", "demoted", "idle"]
)
def test_invalid_current_session_or_command_is_denied_without_consumption(
    ready: ReauthSetup, mutation: str
) -> None:
    database, deps, _runtime, value, token, _secret, _session = ready
    args: dict[str, Any] = {}
    if mutation == "actor":
        args["binding"] = replace(value, actor_id=str(uuid4()))
    elif mutation == "attempt":
        args["attempt_id"] = "wrong"
    elif mutation == "token":
        args["raw_session"] = "not-an-issued-session"
    elif mutation == "idle":
        cast(MutableClock, deps.clock).value += timedelta(days=2)
    else:
        updates = {
            "revoked": (
                "UPDATE identity.session SET revoked_at=now(), revoke_reason='TEST' "
                "WHERE kind='FULL'"
            ),
            "bootstrap": (
                "UPDATE identity.session SET kind='BOOTSTRAP', "
                "bootstrap_purpose='PASSWORD_RESET' WHERE kind='FULL'"
            ),
            "disabled": "UPDATE identity.account SET status='DISABLED'",
            "demoted": "UPDATE identity.account SET is_super_admin=false",
        }
        with database.owner.begin() as db:
            db.execute(text(updates[mutation]))
    with pytest.raises(identity.PolicyReauthenticationDenied):
        consume(ready, **args)
    with database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 0
        )


def test_invalid_totp_failure_limit_survives_errors_across_attempts(ready: ReauthSetup) -> None:
    database, deps, _runtime, value, _token, _secret, _session = ready
    with database.runtime.connect() as db:
        cap = deps.policy.get_identity_policy(db).totp_attempt_cap
    for index in range(cap + 1):
        attempt = f"failed-{index}"
        with pytest.raises(identity.PolicyReauthenticationDenied):
            consume(
                ready,
                binding=replace(value, command_attempt_id=attempt),
                attempt_id=attempt,
                totp_code="invalid",
            )
    with database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT sum(attempt_count) FROM identity.auth_challenge "
                    "WHERE purpose='POLICY_PUBLISH'"
                )
            ).scalar_one()
            == cap
        )
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 0
        )
    with pytest.raises(identity.PolicyReauthenticationDenied):
        consume(ready)


def test_same_totp_is_consumed_once_across_concurrent_different_commands(
    ready: ReauthSetup,
) -> None:
    database, _deps, _runtime, value, _token, _secret, _session = ready
    barrier = Barrier(2)

    def run(attempt: str) -> identity.ConsumedReauthReceipt | None:
        barrier.wait()
        try:
            return consume(
                ready, binding=replace(value, command_attempt_id=attempt), attempt_id=attempt
            )
        except identity.PolicyReauthenticationDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, ["concurrent-1", "concurrent-2"]))
    assert sum(result is not None for result in outcomes) == 1
    with database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 1
        )
        assert (
            db.execute(
                text(
                    "SELECT sum(attempt_count) FROM identity.auth_challenge "
                    "WHERE purpose='POLICY_PUBLISH'"
                )
            ).scalar_one()
            == 1
        )


def test_success_is_not_reissued_even_with_new_totp_on_same_attempt(ready: ReauthSetup) -> None:
    _database, deps, _runtime, _value, _token, _secret, _session = ready
    consume(ready)
    cast(MutableClock, deps.clock).value += timedelta(seconds=30)
    with pytest.raises(identity.PolicyReauthenticationDenied):
        consume(ready)


@pytest.mark.parametrize(
    "mutation", ["binding", "session", "version", "expiry", "forged", "revoked"]
)
def test_final_validation_rejects_drift_without_reactivating_consumption(
    ready: ReauthSetup, mutation: str
) -> None:
    database, deps, runtime, value, token, _secret, _session = ready
    receipt = consume(ready)
    if mutation == "binding":
        value = replace(value, operation="POLICY_ROLLBACK")
    elif mutation == "session":
        token = "untrusted-session"
    elif mutation == "expiry":
        cast(MutableClock, deps.clock).value += timedelta(minutes=5)
    elif mutation == "forged":
        receipt = replace(receipt, receipt_id=str(uuid4()))
    else:
        with database.owner.begin() as db:
            db.execute(
                text(
                    "UPDATE identity.account SET version=version+1"
                    if mutation == "version"
                    else "UPDATE identity.session SET revoked_at=now(), revoke_reason='TEST' "
                    "WHERE kind='FULL'"
                )
            )
    with pytest.raises(identity.PolicyReauthenticationDenied):
        runtime.validate_consumed_policy_reauth(raw_session=token, binding=value, receipt=receipt)
    with database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 1
        )


def test_rollback_consumes_its_distinct_operation(ready: ReauthSetup) -> None:
    database, _deps, _runtime, value, _token, _secret, _session = ready
    receipt = consume(ready, binding=replace(value, operation="POLICY_ROLLBACK"))
    assert receipt.binding.operation == "POLICY_ROLLBACK"
    with database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT purpose FROM identity.auth_challenge "
                    "WHERE consumed_at IS NOT NULL AND purpose='POLICY_ROLLBACK'"
                )
            ).scalar_one()
            == "POLICY_ROLLBACK"
        )


def test_consumed_facts_are_append_only_and_inaccessible_to_other_owners(
    ready: ReauthSetup,
) -> None:
    database, *_rest = ready
    consume(ready)
    for statement in (
        "UPDATE identity.policy_reauth_consumption SET expires_at=now()",
        "DELETE FROM identity.policy_reauth_consumption",
    ):
        with database.runtime.begin() as db, pytest.raises(ProgrammingError):
            db.execute(text(statement))
    with database.owner.connect() as db:
        for role in ("requirement_rw", "configuration_rw"):
            assert not db.execute(
                text(
                    "SELECT has_table_privilege(:role, "
                    "'identity.policy_reauth_consumption', 'SELECT')"
                ),
                {"role": role},
            ).scalar_one()


@pytest.mark.parametrize("committed", [False, True])
def test_unknown_commit_never_returns_receipt_or_exposes_exception_material(
    ready: ReauthSetup, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    database, _deps, _runtime, _value, _token, _secret, _session = ready
    original = database.runtime.begin

    @contextmanager
    def uncertain() -> Iterator[Connection]:
        with original() as db:
            yield db
            if not committed:
                raise RuntimeError("private-transport-detail")
        raise RuntimeError("private-transport-detail")

    monkeypatch.setattr(database.runtime, "begin", uncertain)
    with pytest.raises(identity.PolicyReauthenticationUnavailable) as error:
        consume(ready)
    assert "private-transport-detail" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    with database.owner.connect() as db:
        assert db.execute(
            text("SELECT count(*) FROM identity.policy_reauth_consumption")
        ).scalar_one() == int(committed)


def test_audit_failure_rolls_back_consumption_and_returns_no_receipt(ready: ReauthSetup) -> None:
    database, *_rest = ready

    def fail_audit(
        _conn: object,
        _cursor: object,
        statement: str,
        parameters: Any,
        _context: object,
        _many: object,
    ) -> None:
        if (
            "audit.append_event" in statement
            and parameters.get("action") == "identity.policy_reauth.consumed"
        ):
            raise RuntimeError("private-audit-detail")

    event.listen(database.runtime, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(identity.PolicyReauthenticationUnavailable):
            consume(ready)
    finally:
        event.remove(database.runtime, "before_cursor_execute", fail_audit)
    with database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 0
        )


def test_final_expiry_is_checked_after_persisted_fact_read(ready: ReauthSetup) -> None:
    database, deps, runtime, value, token, _secret, _session = ready
    receipt = consume(ready)

    def delay_fact_read(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: object,
    ) -> None:
        if "SELECT EXISTS" in statement and "policy_reauth_consumption" in statement:
            cast(MutableClock, deps.clock).value += timedelta(minutes=5)

    event.listen(database.runtime, "after_cursor_execute", delay_fact_read)
    try:
        with pytest.raises(identity.PolicyReauthenticationDenied):
            runtime.validate_consumed_policy_reauth(
                raw_session=token, binding=value, receipt=receipt
            )
    finally:
        event.remove(database.runtime, "after_cursor_execute", delay_fact_read)


@pytest.mark.parametrize("mutation", ["missing", "corrupt", "unsupported"])
def test_unavailable_identity_policy_never_issues_receipt(
    ready: ReauthSetup, mutation: str
) -> None:
    database, *_rest = ready
    statements = {
        "missing": "DELETE FROM identity.active_pointer",
        "corrupt": "UPDATE identity.version SET snapshot_hash=repeat('f',64)",
        "unsupported": "UPDATE identity.version SET schema_revision=999",
    }
    with database.owner.begin() as db:
        db.execute(text(statements[mutation]))
    with pytest.raises(identity.PolicyReauthenticationUnavailable):
        consume(ready)
    with database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 0
        )
