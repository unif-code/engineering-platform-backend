import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("kind", "attempt_count"),
    [("BOOTSTRAP", -1), ("FULL", 1)],
)
def test_bootstrap_totp_attempt_count_rejects_invalid_session_state(
    identity_owner_engine: Engine,
    clean_identity_db: None,
    kind: str,
    attempt_count: int,
) -> None:
    with identity_owner_engine.begin() as db:
        account_id = db.execute(
            text(
                "INSERT INTO identity.account "
                "(id, employee_no, display_name, status) VALUES "
                "('00000000-0000-0000-0000-000000000001', "
                "'00000001', 'Alice', 'PENDING_INIT') RETURNING id"
            )
        ).scalar_one()
    purpose = "INITIAL_SETUP" if kind == "BOOTSTRAP" else None
    with identity_owner_engine.connect() as db:
        transaction = db.begin()
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO identity.session "
                    "(id, account_id, token_hash, kind, bootstrap_purpose, "
                    "bootstrap_totp_attempt_count, expires_hint) VALUES "
                    "('00000000-0000-0000-0000-000000000002', :account_id, "
                    "'task6-invalid-attempts', :kind, :purpose, :attempt_count, "
                    "now() + interval '1 hour')"
                ),
                {
                    "account_id": account_id,
                    "kind": kind,
                    "purpose": purpose,
                    "attempt_count": attempt_count,
                },
            )
        transaction.rollback()


def test_identity_rw_can_update_bootstrap_totp_attempt_count(
    identity_rw_engine: Engine,
) -> None:
    with identity_rw_engine.begin() as db:
        db.execute(text("UPDATE identity.session SET bootstrap_totp_attempt_count=0 WHERE false"))


def test_identity_0004_downgrade_and_upgrade_are_reversible(
    identity_owner_engine: Engine,
) -> None:
    config = Config("alembic.ini")
    try:
        command.downgrade(config, "0003_identity_bootstrap_purpose")
        with identity_owner_engine.connect() as db:
            downgraded = db.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='identity' AND table_name='session' "
                    "AND column_name='bootstrap_totp_attempt_count'"
                )
            ).scalar_one()
        assert downgraded == 0
    finally:
        command.upgrade(config, "heads")

    with identity_owner_engine.connect() as db:
        upgraded = db.execute(
            text(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_schema='identity' AND table_name='session' "
                "AND column_name='bootstrap_totp_attempt_count'"
            )
        ).one()
    assert upgraded.column_default == "0"
    assert upgraded.is_nullable == "NO"


def test_identity_0004_upgrade_revokes_untracked_bootstrap_attempt_state(
    identity_owner_engine: Engine,
    clean_identity_db: None,
) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0003_identity_bootstrap_purpose")
    try:
        with identity_owner_engine.begin() as db:
            account_id = db.execute(
                text(
                    "INSERT INTO identity.account "
                    "(id, employee_no, display_name, status) VALUES "
                    "('00000000-0000-0000-0000-000000000001', "
                    "'00000001', 'Alice', 'PENDING_INIT') RETURNING id"
                )
            ).scalar_one()
            db.execute(
                text(
                    "INSERT INTO identity.session "
                    "(id, account_id, token_hash, kind, bootstrap_purpose, expires_hint, "
                    "revoked_at, revoke_reason) VALUES "
                    "('00000000-0000-0000-0000-000000000011', :account_id, "
                    "'task6-upgrade-active', 'BOOTSTRAP', 'INITIAL_SETUP', "
                    "now() + interval '1 hour', NULL, NULL), "
                    "('00000000-0000-0000-0000-000000000012', :account_id, "
                    "'task6-upgrade-revoked', 'BOOTSTRAP', 'INITIAL_SETUP', "
                    "now() + interval '1 hour', now(), 'PREEXISTING_REASON')"
                ),
                {"account_id": account_id},
            )

        command.upgrade(config, "heads")
        with identity_owner_engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT token_hash, bootstrap_totp_attempt_count, revoked_at, "
                    "revoke_reason FROM identity.session ORDER BY token_hash"
                )
            ).mappings()
            by_token = {row["token_hash"]: row for row in rows}

        assert by_token["task6-upgrade-active"]["bootstrap_totp_attempt_count"] == 0
        assert by_token["task6-upgrade-active"]["revoked_at"] is not None
        assert (
            by_token["task6-upgrade-active"]["revoke_reason"]
            == "MIGRATION_BOOTSTRAP_TOTP_ATTEMPTS_UPGRADE"
        )
        assert by_token["task6-upgrade-revoked"]["revoke_reason"] == "PREEXISTING_REASON"
    finally:
        command.upgrade(config, "heads")


def test_identity_0004_downgrade_revokes_active_bootstrap_sessions(
    identity_owner_engine: Engine,
    clean_identity_db: None,
) -> None:
    with identity_owner_engine.begin() as db:
        account_id = db.execute(
            text(
                "INSERT INTO identity.account "
                "(id, employee_no, display_name, status) VALUES "
                "('00000000-0000-0000-0000-000000000001', "
                "'00000001', 'Alice', 'PENDING_INIT') RETURNING id"
            )
        ).scalar_one()
        db.execute(
            text(
                "INSERT INTO identity.session "
                "(id, account_id, token_hash, kind, bootstrap_purpose, expires_hint, "
                "bootstrap_totp_attempt_count, revoked_at, revoke_reason) VALUES "
                "('00000000-0000-0000-0000-000000000021', :account_id, "
                "'task6-downgrade-active', 'BOOTSTRAP', 'INITIAL_SETUP', "
                "now() + interval '1 hour', 2, NULL, NULL), "
                "('00000000-0000-0000-0000-000000000022', :account_id, "
                "'task6-downgrade-revoked', 'BOOTSTRAP', 'INITIAL_SETUP', "
                "now() + interval '1 hour', 1, now(), 'PREEXISTING_REASON')"
            ),
            {"account_id": account_id},
        )

    config = Config("alembic.ini")
    try:
        command.downgrade(config, "0003_identity_bootstrap_purpose")
        with identity_owner_engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT token_hash, revoked_at, revoke_reason "
                    "FROM identity.session ORDER BY token_hash"
                )
            ).mappings()
            by_token = {row["token_hash"]: row for row in rows}

        assert by_token["task6-downgrade-active"]["revoked_at"] is not None
        assert (
            by_token["task6-downgrade-active"]["revoke_reason"]
            == "MIGRATION_BOOTSTRAP_TOTP_ATTEMPTS_DOWNGRADE"
        )
        assert by_token["task6-downgrade-revoked"]["revoke_reason"] == "PREEXISTING_REASON"
    finally:
        command.upgrade(config, "heads")
