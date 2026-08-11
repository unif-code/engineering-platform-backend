import hashlib
import importlib
import io
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine.base import RootTransaction

import control_plane.app.modules.identity as identity
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import _initialize_account

pytestmark = pytest.mark.integration


class FailingWriter:
    def write(self, value: str) -> int:
        del value
        raise OSError("simulated stdout failure")


class UnavailableConvergence:
    def complete(self, _ticket: object) -> None:
        raise RuntimeError("injected convergence outage")

    def reconcile_pending(self) -> bool:
        return False


def test_recovery_restricts_last_unavailable_admin_and_atomically_reissues_bootstrap(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed: list[str] = []
    dependencies = replace(
        identity_dependencies(),
        on_auth_change=lambda account_id: changed.append(account_id),
    )
    _secret, old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.begin() as db:
        account_id = str(
            db.execute(
                text(
                    "UPDATE identity.account SET is_super_admin=true, status='DISABLED', "
                    "version=version+1 RETURNING id"
                )
            ).scalar_one()
        )
    changed.clear()
    expires_at = dependencies.clock.now() + timedelta(minutes=15)

    with identity_rw_engine.begin() as db:
        recovered, temporary_password = identity.recover_super_admin(
            db,
            employee_no="00000001",
            reason="approved incident INC-1001",
            scope="SUPER_ADMIN_AUTHENTICATION",
            expires_at=expires_at,
            credentials_lost=False,
            dependencies=dependencies,
        )

    assert recovered.id == account_id
    assert recovered.is_super_admin is True
    assert recovered.status is identity.AccountStatus.PENDING_INIT
    assert changed == [account_id]
    with identity_owner_engine.connect() as db:
        state = (
            db.execute(
                text(
                    "SELECT a.password_hash, a.totp_sealed, a.totp_confirmed_at, "
                    "s.revoked_at, t.secret_hash, t.expires_at "
                    "FROM identity.account a JOIN identity.session s ON s.account_id=a.id "
                    "JOIN identity.temp_credential t ON t.account_id=a.id "
                    "WHERE s.token_hash=:token_hash AND t.consumed_at IS NULL"
                ),
                {"token_hash": hashlib.sha256(old_session.encode()).hexdigest()},
            )
            .mappings()
            .one()
        )
        audit_rows = (
            db.execute(
                text(
                    "SELECT actor, actor_type, action, target_id, result, reason "
                    "FROM audit.audit_event ORDER BY occurred_at, id"
                )
            )
            .mappings()
            .all()
        )
    assert state["password_hash"] is None
    assert state["totp_sealed"] is None
    assert state["totp_confirmed_at"] is None
    assert state["revoked_at"] is not None
    assert state["secret_hash"] != temporary_password
    assert state["expires_at"] == expires_at
    assert audit_rows[-1]["actor"] == "SYSTEM_RECOVERY"
    assert audit_rows[-1]["actor_type"] == "SYSTEM"
    assert audit_rows[-1]["action"] == "identity.super_admin.recovered"
    assert temporary_password not in repr(audit_rows)

    with identity_rw_engine.begin() as db:
        bootstrap = identity.consume_temp_password(
            db,
            employee_no="00000001",
            temp_password=temporary_password,
            dependencies=dependencies,
        )
    assert bootstrap is not None
    assert bootstrap.kind is identity.SessionKind.BOOTSTRAP
    assert bootstrap.bootstrap_purpose is identity.BootstrapPurpose.INITIAL_SETUP


def test_recovery_cli_emits_password_once_and_credential_safe_structured_evidence(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _secret, _old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, status='DISABLED', "
                "version=version+1"
            )
        )
    stdout = io.StringIO()
    stderr = io.StringIO()
    recovery_cli = importlib.import_module("control_plane.tools.recovery")

    exit_code = recovery_cli.main(
        [
            "--employee-no",
            "00000001",
            "--reason",
            "approved incident INC-1001",
            "--scope",
            "SUPER_ADMIN_AUTHENTICATION",
            "--expires-at",
            "2026-01-01T00:15:00+00:00",
        ],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    password_lines = stdout.getvalue().splitlines()
    assert len(password_lines) == 1
    assert len(password_lines[0]) >= 24
    evidence_lines = stderr.getvalue().splitlines()
    assert len(evidence_lines) == 1
    evidence = json.loads(evidence_lines[0])
    assert evidence == {
        "event": "super_admin_recovery",
        "result": "SUCCESS",
        "employeeNo": "00000001",
        "scope": "SUPER_ADMIN_AUTHENTICATION",
        "expiresAt": "2026-01-01T00:15:00+00:00",
        "commandId": evidence["commandId"],
    }
    assert evidence["commandId"].startswith("cli-")
    assert password_lines[0] not in stderr.getvalue()
    with identity_owner_engine.connect() as db:
        persisted = db.execute(
            text(
                "SELECT t.secret_hash, e.reason, e.correlation_id FROM "
                "identity.temp_credential t "
                "JOIN audit.audit_event e ON e.target_id=t.account_id::text "
                "WHERE t.consumed_at IS NULL AND e.action='identity.super_admin.recovered'"
            )
        ).one()
    assert password_lines[0] != persisted.secret_hash
    assert password_lines[0] not in persisted.reason
    assert persisted.correlation_id == evidence["commandId"]
    with identity_owner_engine.connect() as db:
        correlations = (
            db.execute(
                text(
                    "SELECT DISTINCT correlation_id FROM audit.audit_event "
                    "WHERE actor='SYSTEM_RECOVERY'"
                )
            )
            .scalars()
            .all()
        )
    assert correlations == [evidence["commandId"]]


def test_recovery_cli_denial_is_nonzero_and_does_not_partially_execute(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _secret, _old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_rw_engine.begin() as db:
        other, _temporary_password = identity.create_account(
            db,
            employee_no="00000002",
            display_name="Other Admin",
            actor=identity.Principal(employee_id="SYSTEM", name="System"),
            reason="recovery denial fixture",
            dependencies=dependencies,
        )
    with identity_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, status='DISABLED', "
                "version=version+1 WHERE employee_no='00000001'"
            )
        )
        db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, status='ENABLED', "
                "password_hash='initialized', totp_confirmed_at=now(), version=version+1 "
                "WHERE id=:account_id"
            ),
            {"account_id": other.id},
        )
        before = (
            db.execute(
                text(
                    "SELECT (SELECT count(*) FROM identity.temp_credential) AS temps, "
                    "(SELECT count(*) FROM audit.audit_event) AS audits"
                )
            )
            .mappings()
            .one()
        )
    stdout = io.StringIO()
    stderr = io.StringIO()
    recovery_cli = importlib.import_module("control_plane.tools.recovery")

    exit_code = recovery_cli.main(
        [
            "--employee-no",
            "00000001",
            "--reason",
            "approved incident INC-1002",
            "--scope",
            "SUPER_ADMIN_AUTHENTICATION",
            "--expires-at",
            "2026-01-01T00:15:00+00:00",
        ],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 3
    assert stdout.getvalue() == ""
    evidence = json.loads(stderr.getvalue())
    assert evidence["event"] == "super_admin_recovery"
    assert evidence["result"] == "DENIED"
    assert "password" not in stderr.getvalue().lower()
    with identity_owner_engine.connect() as db:
        after = (
            db.execute(
                text(
                    "SELECT (SELECT count(*) FROM identity.temp_credential) AS temps, "
                    "(SELECT count(*) FROM audit.audit_event) AS audits, "
                    "(SELECT status FROM identity.account "
                    "WHERE employee_no='00000001') AS status"
                )
            )
            .mappings()
            .one()
        )
    assert after["temps"] == before["temps"]
    assert after["audits"] == before["audits"]
    assert after["status"] == "DISABLED"


def test_recovery_credentials_lost_attestation_covers_multiple_apparently_effective_admins(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _first_secret, first_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000001",
        display_name="Alice",
    )
    _second_secret, _second_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000002",
        display_name="Bob",
    )
    with identity_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                "WHERE employee_no IN ('00000001', '00000002')"
            )
        )

    with identity_rw_engine.begin() as db:
        recovered, temporary_password = identity.recover_super_admin(
            db,
            employee_no="00000001",
            reason="approved incident INC-ALL-CREDENTIALS-LOST",
            scope="SUPER_ADMIN_AUTHENTICATION",
            expires_at=dependencies.clock.now() + timedelta(minutes=15),
            credentials_lost=True,
            dependencies=dependencies,
        )

    assert recovered.status is identity.AccountStatus.PENDING_INIT
    assert len(temporary_password) >= 24
    with identity_owner_engine.connect() as db:
        evidence = (
            db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM identity.account WHERE is_super_admin=true "
                    "AND status='ENABLED' AND password_hash IS NOT NULL "
                    "AND totp_confirmed_at IS NOT NULL) AS effective_admins, "
                    "(SELECT revoked_at IS NOT NULL FROM identity.session "
                    "WHERE token_hash=:token_hash) AS target_session_revoked, "
                    "(SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.super_admin.recovered' AND result='SUCCESS') "
                    "AS recovery_audits"
                ),
                {"token_hash": hashlib.sha256(first_session.encode()).hexdigest()},
            )
            .mappings()
            .one()
        )
    assert dict(evidence) == {
        "effective_admins": 1,
        "target_session_revoked": True,
        "recovery_audits": 1,
    }


def test_recovery_cli_output_failure_rolls_back_all_database_changes(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _secret, old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.begin() as db:
        before = (
            db.execute(
                text(
                    "UPDATE identity.account SET is_super_admin=true, status='DISABLED', "
                    "version=version+1 WHERE employee_no='00000001' "
                    "RETURNING password_hash, totp_sealed, version"
                )
            )
            .mappings()
            .one()
        )
        before_counts = (
            db.execute(
                text(
                    "SELECT (SELECT count(*) FROM identity.temp_credential) AS temps, "
                    "(SELECT count(*) FROM audit.audit_event) AS audits"
                )
            )
            .mappings()
            .one()
        )
    stderr = io.StringIO()
    recovery_cli = importlib.import_module("control_plane.tools.recovery")

    exit_code = recovery_cli.main(
        [
            "--employee-no",
            "00000001",
            "--reason",
            "approved incident INC-1003",
            "--scope",
            "SUPER_ADMIN_AUTHENTICATION",
            "--expires-at",
            "2026-01-01T00:15:00+00:00",
        ],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=FailingWriter(),
        stderr=stderr,
    )

    assert exit_code == 4
    evidence = json.loads(stderr.getvalue())
    assert evidence == {"event": "super_admin_recovery", "result": "FAILED"}
    with identity_owner_engine.connect() as db:
        after = (
            db.execute(
                text(
                    "SELECT a.password_hash, a.totp_sealed, a.version, s.revoked_at, "
                    "(SELECT count(*) FROM identity.temp_credential) AS temps, "
                    "(SELECT count(*) FROM audit.audit_event) AS audits "
                    "FROM identity.account a JOIN identity.session s ON s.account_id=a.id "
                    "WHERE a.employee_no='00000001' AND s.token_hash=:token_hash"
                ),
                {"token_hash": hashlib.sha256(old_session.encode()).hexdigest()},
            )
            .mappings()
            .one()
        )
    assert after["password_hash"] == before["password_hash"]
    assert after["totp_sealed"] == before["totp_sealed"]
    assert after["version"] == before["version"]
    assert after["revoked_at"] is None
    assert after["temps"] == before_counts["temps"]
    assert after["audits"] == before_counts["audits"]


def test_recovery_cli_committed_change_never_reports_nonzero_for_pending_convergence(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = replace(
        identity_dependencies(),
        on_auth_change=lambda _account_id: object(),
    )
    _secret, _old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, status='DISABLED', "
                "version=version+1"
            )
        )
    stdout = io.StringIO()
    stderr = io.StringIO()
    recovery_cli = importlib.import_module("control_plane.tools.recovery")

    exit_code = recovery_cli.main(
        [
            "--employee-no",
            "00000001",
            "--reason",
            "approved incident INC-1004",
            "--scope",
            "SUPER_ADMIN_AUTHENTICATION",
            "--expires-at",
            "2026-01-01T00:15:00+00:00",
        ],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=UnavailableConvergence(),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert len(stdout.getvalue().splitlines()) == 1
    assert json.loads(stderr.getvalue())["result"] == "SUCCESS"
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text("SELECT status FROM identity.account WHERE employee_no='00000001'")
            ).scalar_one()
            == "PENDING_INIT"
        )


def test_recovery_cli_commit_ack_loss_resolves_claim_and_replays_same_credential(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _secret, _old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, status='DISABLED', "
                "version=version+1"
            )
        )
    original_commit = RootTransaction.commit
    lose_ack_once = True

    def commit_then_lose_ack(transaction: RootTransaction) -> None:
        nonlocal lose_ack_once
        original_commit(transaction)
        if lose_ack_once:
            lose_ack_once = False
            raise OSError("simulated lost commit acknowledgement")

    monkeypatch.setattr(RootTransaction, "commit", commit_then_lose_ack)
    recovery_cli = importlib.import_module("control_plane.tools.recovery")
    argv = [
        "--employee-no",
        "00000001",
        "--reason",
        "approved incident INC-1005",
        "--scope",
        "SUPER_ADMIN_AUTHENTICATION",
        "--expires-at",
        "2026-01-01T00:15:00+00:00",
    ]
    first_stdout = io.StringIO()
    first_stderr = io.StringIO()

    first_exit = recovery_cli.main(
        argv,
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=first_stdout,
        stderr=first_stderr,
    )
    replay_stdout = io.StringIO()
    replay_stderr = io.StringIO()
    replay_exit = recovery_cli.main(
        argv,
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=replay_stdout,
        stderr=replay_stderr,
    )

    assert first_exit == 0
    assert replay_exit == 0
    assert first_stdout.getvalue() == replay_stdout.getvalue()
    assert len(first_stdout.getvalue().splitlines()) == 1
    assert json.loads(first_stderr.getvalue())["result"] == "SUCCESS"
    assert json.loads(replay_stderr.getvalue())["result"] == "SUCCESS"
    with identity_owner_engine.connect() as db:
        evidence = (
            db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM identity.temp_credential "
                    "WHERE consumed_at IS NULL) AS temps, "
                    "(SELECT count(*) FROM identity.idempotency_record "
                    "WHERE operation='super_admin_recovery_cli') AS claims, "
                    "(SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.super_admin.recovered') AS recoveries"
                )
            )
            .mappings()
            .one()
        )
    assert dict(evidence) == {"temps": 1, "claims": 1, "recoveries": 1}


def test_recovery_cli_commit_ack_loss_and_resolution_outage_never_reports_nonzero(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _secret, _old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, status='DISABLED', "
                "version=version+1"
            )
        )
    original_commit = RootTransaction.commit
    lose_ack_once = True

    def commit_then_lose_ack(transaction: RootTransaction) -> None:
        nonlocal lose_ack_once
        original_commit(transaction)
        if lose_ack_once:
            lose_ack_once = False
            raise OSError("simulated lost commit acknowledgement")

    recovery_cli = importlib.import_module("control_plane.tools.recovery")
    monkeypatch.setattr(RootTransaction, "commit", commit_then_lose_ack)
    monkeypatch.setattr(
        recovery_cli,
        "_resolve_committed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("resolution outage")),
    )
    monkeypatch.setattr(recovery_cli, "_transaction_status", lambda *_args: "unknown")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = recovery_cli.main(
        [
            "--employee-no",
            "00000001",
            "--reason",
            "approved incident INC-1006",
            "--scope",
            "SUPER_ADMIN_AUTHENTICATION",
            "--expires-at",
            "2026-01-01T00:15:00+00:00",
        ],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert len(stdout.getvalue().splitlines()) == 1
    assert json.loads(stderr.getvalue())["result"] == "OUTCOME_UNKNOWN"
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text("SELECT status FROM identity.account WHERE employee_no='00000001'")
            ).scalar_one()
            == "PENDING_INIT"
        )


def test_recovery_cli_committed_change_ignores_stderr_delivery_failure(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _secret, _old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, status='DISABLED', "
                "version=version+1"
            )
        )

    recovery_cli = importlib.import_module("control_plane.tools.recovery")
    stdout = io.StringIO()
    exit_code = recovery_cli.main(
        [
            "--employee-no",
            "00000001",
            "--reason",
            "approved incident INC-1007",
            "--scope",
            "SUPER_ADMIN_AUTHENTICATION",
            "--expires-at",
            "2026-01-01T00:15:00+00:00",
        ],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=stdout,
        stderr=FailingWriter(),
    )

    assert exit_code == 0
    assert len(stdout.getvalue().splitlines()) == 1
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text("SELECT status FROM identity.account WHERE employee_no='00000001'")
            ).scalar_one()
            == "PENDING_INIT"
        )


def test_bootstrap_cli_succeeds_once_and_never_persists_plaintext_password(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    dependencies = identity_dependencies()
    bootstrap_cli = importlib.import_module("control_plane.tools.bootstrap_admin")
    stdout = io.StringIO()
    stderr = io.StringIO()

    first_exit = bootstrap_cli.main(
        ["--employee-no", "00000001", "--display-name", "张三"],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=stdout,
        stderr=stderr,
    )

    assert first_exit == 0
    password_lines = stdout.getvalue().splitlines()
    assert len(password_lines) == 1
    temporary_password = password_lines[0]
    assert len(temporary_password) >= 24
    assert json.loads(stderr.getvalue()) == {
        "event": "super_admin_bootstrap",
        "result": "SUCCESS",
        "employeeNo": "00000001",
        "commandId": json.loads(stderr.getvalue())["commandId"],
    }
    command_id = json.loads(stderr.getvalue())["commandId"]
    assert command_id.startswith("cli-")
    second_stdout = io.StringIO()
    second_stderr = io.StringIO()
    second_exit = bootstrap_cli.main(
        ["--employee-no", "00000002", "--display-name", "李四"],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=second_stdout,
        stderr=second_stderr,
    )
    assert second_exit == 3
    assert second_stdout.getvalue() == ""
    assert json.loads(second_stderr.getvalue()) == {
        "event": "super_admin_bootstrap",
        "result": "DENIED",
    }

    with identity_owner_engine.connect() as db:
        persisted = (
            db.execute(
                text(
                    "SELECT a.password_hash, t.secret_hash, e.actor, e.actor_type, e.reason, "
                    "e.correlation_id, "
                    "(SELECT count(*) FROM identity.account WHERE is_super_admin) AS admins "
                    "FROM identity.account a "
                    "JOIN identity.temp_credential t ON t.account_id=a.id "
                    "JOIN audit.audit_event e ON e.target_id=a.id::text "
                    "WHERE e.action='identity.super_admin.bootstrapped'"
                )
            )
            .mappings()
            .one()
        )
    assert persisted["admins"] == 1
    assert persisted["actor"] == "SYSTEM_BOOTSTRAP"
    assert persisted["actor_type"] == "SYSTEM"
    assert persisted["correlation_id"] == command_id
    assert temporary_password not in str(persisted["password_hash"])
    assert temporary_password != persisted["secret_hash"]
    assert temporary_password not in persisted["reason"]


def test_bootstrap_cli_commit_ack_loss_resolves_claim_and_replays_same_credential(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    original_commit = RootTransaction.commit
    lose_ack_once = True

    def commit_then_lose_ack(transaction: RootTransaction) -> None:
        nonlocal lose_ack_once
        original_commit(transaction)
        if lose_ack_once:
            lose_ack_once = False
            raise OSError("simulated lost commit acknowledgement")

    monkeypatch.setattr(RootTransaction, "commit", commit_then_lose_ack)
    bootstrap_cli = importlib.import_module("control_plane.tools.bootstrap_admin")
    argv = ["--employee-no", "00000001", "--display-name", "Alice"]
    first_stdout = io.StringIO()
    first_stderr = io.StringIO()

    first_exit = bootstrap_cli.main(
        argv,
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=first_stdout,
        stderr=first_stderr,
    )
    replay_stdout = io.StringIO()
    replay_stderr = io.StringIO()
    replay_exit = bootstrap_cli.main(
        argv,
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=replay_stdout,
        stderr=replay_stderr,
    )

    assert first_exit == 0
    assert replay_exit == 0
    assert first_stdout.getvalue() == replay_stdout.getvalue()
    assert len(first_stdout.getvalue().splitlines()) == 1
    assert json.loads(first_stderr.getvalue())["result"] == "SUCCESS"
    assert json.loads(replay_stderr.getvalue())["result"] == "SUCCESS"
    with identity_owner_engine.connect() as db:
        evidence = (
            db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM identity.account "
                    "WHERE is_super_admin=true) AS admins, "
                    "(SELECT count(*) FROM identity.idempotency_record "
                    "WHERE operation='super_admin_bootstrap_cli') AS claims, "
                    "(SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.super_admin.bootstrapped') AS bootstraps"
                )
            )
            .mappings()
            .one()
        )
    assert dict(evidence) == {"admins": 1, "claims": 1, "bootstraps": 1}


def test_bootstrap_cli_commit_ack_loss_and_resolution_outage_never_reports_nonzero(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    original_commit = RootTransaction.commit
    lose_ack_once = True

    def commit_then_lose_ack(transaction: RootTransaction) -> None:
        nonlocal lose_ack_once
        original_commit(transaction)
        if lose_ack_once:
            lose_ack_once = False
            raise OSError("simulated lost commit acknowledgement")

    bootstrap_cli = importlib.import_module("control_plane.tools.bootstrap_admin")
    monkeypatch.setattr(RootTransaction, "commit", commit_then_lose_ack)
    monkeypatch.setattr(
        bootstrap_cli,
        "_resolve_committed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("resolution outage")),
    )
    monkeypatch.setattr(bootstrap_cli, "_transaction_status", lambda *_args: "unknown")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = bootstrap_cli.main(
        ["--employee-no", "00000001", "--display-name", "Alice"],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert len(stdout.getvalue().splitlines()) == 1
    assert json.loads(stderr.getvalue())["result"] == "OUTCOME_UNKNOWN"
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM identity.account WHERE is_super_admin=true")
            ).scalar_one()
            == 1
        )


def test_bootstrap_cli_committed_change_ignores_stderr_delivery_failure(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    dependencies = identity_dependencies()
    bootstrap_cli = importlib.import_module("control_plane.tools.bootstrap_admin")
    stdout = io.StringIO()

    exit_code = bootstrap_cli.main(
        ["--employee-no", "00000001", "--display-name", "Alice"],
        engine=identity_rw_engine,
        dependencies=dependencies,
        security_changes=None,
        stdout=stdout,
        stderr=FailingWriter(),
    )

    assert exit_code == 0
    assert len(stdout.getvalue().splitlines()) == 1
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM identity.account WHERE is_super_admin=true")
            ).scalar_one()
            == 1
        )
