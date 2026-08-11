import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.identity.domain.errors import AccountConflict


class SqlAlchemyIdentityRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def lock_super_admin_invariant(self) -> None:
        self.db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('identity.effective_super_admin', 0))"
            )
        )

    def lock_backoff_scope(self, employee_no: str, source: str) -> None:
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope_key, 0))"),
            {"scope_key": f"identity.backoff:{employee_no}:{source}"},
        )

    def lock_account_lifecycle(self, account_id: str) -> None:
        self.db.execute(
            text("SELECT id FROM identity.account WHERE id=:account_id FOR UPDATE"),
            {"account_id": account_id},
        )

    def claim_idempotency(self, **values: Any) -> bool:
        result = self.db.execute(
            text(
                "INSERT INTO identity.idempotency_record "
                "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                "created_at, updated_at) VALUES "
                "(:id, :actor, :operation, :idempotency_key, :request_fingerprint, "
                "'IN_PROGRESS', :now, :now) "
                "ON CONFLICT (actor, operation, idempotency_key) DO NOTHING RETURNING id"
            ),
            values,
        )
        return result.scalar_one_or_none() is not None

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM identity.idempotency_record "
                    "WHERE actor=:actor AND operation=:operation "
                    f"AND idempotency_key=:idempotency_key{suffix}"
                ),
                {
                    "actor": actor,
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                },
            )
            .mappings()
            .one_or_none()
        )

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool:
        result = self.db.execute(
            text(
                "UPDATE identity.idempotency_record SET state='COMPLETED', "
                "http_status=:http_status, result_metadata=CAST(:result_metadata AS JSONB), "
                "sealed_response=:sealed_response, completed_at=:now, updated_at=:now "
                "WHERE id=:id AND state='IN_PROGRESS'"
            ),
            {
                "id": record_id,
                "http_status": http_status,
                "result_metadata": json.dumps(result_metadata, separators=(",", ":")),
                "sealed_response": sealed_response,
                "now": now,
            },
        )
        return result.rowcount == 1

    def insert_account(self, **values: Any) -> None:
        try:
            self.db.execute(
                text(
                    "INSERT INTO identity.account "
                    "(id, employee_no, display_name, profession, status, version, "
                    "created_at, updated_at) "
                    "VALUES (:id, :employee_no, :display_name, :profession, "
                    ":status, 1, :now, :now)"
                ),
                values,
            )
        except IntegrityError as exc:
            raise AccountConflict("employee number already exists") from exc

    def account_by_id(self, account_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM identity.account WHERE id=:id{suffix}"),
                {"id": account_id},
            )
            .mappings()
            .one_or_none()
        )

    def account_by_employee_no(self, employee_no: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM identity.account WHERE employee_no=:employee_no{suffix}"),
                {"employee_no": employee_no},
            )
            .mappings()
            .one_or_none()
        )

    def invalidate_temp_credentials(self, account_id: str, now: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE identity.temp_credential SET consumed_at=:now "
                "WHERE account_id=:account_id AND consumed_at IS NULL AND expires_at>:now"
            ),
            {"account_id": account_id, "now": now},
        )

    def insert_temp_credential(self, **values: Any) -> None:
        self.db.execute(
            text(
                "INSERT INTO identity.temp_credential "
                "(id, account_id, secret_hash, expires_at, issued_by, created_at) "
                "VALUES (:id, :account_id, :secret_hash, :expires_at, :issued_by, :created_at)"
            ),
            values,
        )

    def lock_active_temp_credential(self, employee_no: str, now: datetime) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT t.id, t.account_id, t.secret_hash, a.totp_confirmed_at "
                    "FROM identity.temp_credential t "
                    "JOIN identity.account a ON a.id=t.account_id "
                    "WHERE a.employee_no=:employee_no AND t.consumed_at IS NULL "
                    "AND t.expires_at>:now ORDER BY t.created_at DESC, t.id DESC "
                    "LIMIT 1 FOR UPDATE OF t SKIP LOCKED"
                ),
                {"employee_no": employee_no, "now": now},
            )
            .mappings()
            .one_or_none()
        )

    def consume_temp_credential(self, credential_id: str, now: datetime) -> bool:
        result = self.db.execute(
            text(
                "UPDATE identity.temp_credential SET consumed_at=:now "
                "WHERE id=:id AND consumed_at IS NULL AND expires_at>:now"
            ),
            {"id": credential_id, "now": now},
        )
        return result.rowcount == 1

    def insert_session(self, **values: Any) -> None:
        self.db.execute(
            text(
                "INSERT INTO identity.session "
                "(id, account_id, token_hash, kind, bootstrap_purpose, "
                "created_at, last_seen_at, expires_hint) "
                "VALUES (:id, :account_id, :token_hash, :kind, :bootstrap_purpose, "
                ":now, :now, :expires_hint)"
            ),
            values,
        )

    def session_with_account(self, token_hash: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE OF s, a" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT s.id AS session_id, s.account_id, s.kind, s.bootstrap_purpose, "
                    "s.created_at, "
                    "s.last_seen_at, s.revoked_at, a.employee_no, a.display_name, a.status, "
                    "a.password_hash, a.password_set_at, a.totp_sealed, a.totp_confirmed_at, "
                    "a.totp_last_step, a.is_super_admin, a.version "
                    "FROM identity.session s JOIN identity.account a ON a.id=s.account_id "
                    f"WHERE s.token_hash=:token_hash{suffix}"
                ),
                {"token_hash": token_hash},
            )
            .mappings()
            .one_or_none()
        )

    def update_password(self, account_id: str, password_hash: str, now: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE identity.account SET password_hash=:password_hash, "
                "password_set_at=:now, updated_at=:now, version=version+1 WHERE id=:account_id"
            ),
            {"account_id": account_id, "password_hash": password_hash, "now": now},
        )

    def reset_password_state(self, account_id: str, now: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE identity.account SET password_hash=NULL, password_set_at=NULL, "
                "status='PENDING_INIT', updated_at=:now, version=version+1 "
                "WHERE id=:account_id"
            ),
            {"account_id": account_id, "now": now},
        )

    def update_totp_enrollment(self, account_id: str, sealed: bytes, now: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE identity.account SET totp_sealed=:sealed, totp_confirmed_at=NULL, "
                "totp_last_step=NULL, updated_at=:now, version=version+1 WHERE id=:account_id"
            ),
            {"account_id": account_id, "sealed": sealed, "now": now},
        )

    def confirm_totp(self, account_id: str, step: int, now: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE identity.account SET totp_confirmed_at=:now, totp_last_step=:step, "
                "status='ENABLED', updated_at=:now, version=version+1 WHERE id=:account_id"
            ),
            {"account_id": account_id, "step": step, "now": now},
        )

    def restore_password_reset(self, account_id: str, step: int, now: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE identity.account SET totp_last_step=:step, status='ENABLED', "
                "updated_at=:now, version=version+1 WHERE id=:account_id "
                "AND password_hash IS NOT NULL AND totp_confirmed_at IS NOT NULL"
            ),
            {"account_id": account_id, "step": step, "now": now},
        )

    def revoke_sessions(
        self,
        account_id: str,
        now: datetime,
        reason: str,
        *,
        except_session_id: str | None = None,
        kind: str | None = None,
    ) -> list[str]:
        conditions = ["account_id=:account_id", "revoked_at IS NULL"]
        if except_session_id is not None:
            conditions.append("id<>:except_session_id")
        if kind is not None:
            conditions.append("kind=:kind")
        result = self.db.execute(
            text(
                "UPDATE identity.session SET revoked_at=:now, revoke_reason=:reason WHERE "
                + " AND ".join(conditions)
                + " RETURNING id"
            ),
            {
                "account_id": account_id,
                "now": now,
                "reason": reason,
                "except_session_id": except_session_id,
                "kind": kind,
            },
        )
        return [str(value) for value in result.scalars()]

    def insert_challenge(self, **values: Any) -> None:
        self.db.execute(
            text(
                "INSERT INTO identity.auth_challenge "
                "(id, token_hash, purpose, account_id, actor_id, issued_at, expires_at, "
                "attempt_limit, attempt_count) VALUES "
                "(:id, :token_hash, :purpose, :account_id, NULL, :issued_at, :expires_at, "
                ":attempt_limit, 0)"
            ),
            values,
        )

    def challenge_by_hash(self, token_hash: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE OF c, a" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT c.id AS challenge_id, c.purpose, c.account_id, c.expires_at, "
                    "c.attempt_limit, c.attempt_count, c.consumed_at, c.revoked_at, "
                    "a.employee_no, a.display_name, a.status, a.totp_sealed, a.totp_last_step, "
                    "a.is_super_admin FROM identity.auth_challenge c "
                    "JOIN identity.account a ON a.id=c.account_id "
                    f"WHERE c.token_hash=:token_hash{suffix}"
                ),
                {"token_hash": token_hash},
            )
            .mappings()
            .one_or_none()
        )

    def fail_challenge(self, challenge_id: str, now: datetime) -> int:
        return (
            self.db.execute(
                text(
                    "UPDATE identity.auth_challenge SET "
                    "attempt_count=LEAST(attempt_count+1, attempt_limit), "
                    "revoked_at=CASE WHEN attempt_count+1>=attempt_limit "
                    "THEN :now ELSE revoked_at END "
                    "WHERE id=:id AND consumed_at IS NULL AND revoked_at IS NULL "
                    "RETURNING attempt_count"
                ),
                {"id": challenge_id, "now": now},
            ).scalar_one_or_none()
            or 0
        )

    def consume_challenge(self, challenge_id: str, now: datetime) -> bool:
        result = self.db.execute(
            text(
                "UPDATE identity.auth_challenge SET consumed_at=:now "
                "WHERE id=:id AND consumed_at IS NULL AND revoked_at IS NULL "
                "AND expires_at>:now AND attempt_count<attempt_limit"
            ),
            {"id": challenge_id, "now": now},
        )
        return result.rowcount == 1

    def update_totp_step(self, account_id: str, step: int, now: datetime) -> bool:
        result = self.db.execute(
            text(
                "UPDATE identity.account SET totp_last_step=:step, updated_at=:now "
                "WHERE id=:account_id AND (totp_last_step IS NULL OR totp_last_step<:step)"
            ),
            {"account_id": account_id, "step": step, "now": now},
        )
        return result.rowcount == 1

    def backoff_by_scope(self, employee_no: str, source: str, *, for_update: bool) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM identity.login_backoff "
                    f"WHERE employee_no=:employee_no AND source=:source{suffix}"
                ),
                {"employee_no": employee_no, "source": source},
            )
            .mappings()
            .one_or_none()
        )

    def save_backoff(
        self,
        employee_no: str,
        source: str,
        failure_count: int,
        last_failure_at: datetime | None,
        locked_until: datetime | None,
    ) -> None:
        self.db.execute(
            text(
                "INSERT INTO identity.login_backoff "
                "(employee_no, source, failure_count, last_failure_at, locked_until) "
                "VALUES (:employee_no, :source, :failure_count, :last_failure_at, :locked_until) "
                "ON CONFLICT (employee_no, source) DO UPDATE SET "
                "failure_count=EXCLUDED.failure_count, "
                "last_failure_at=EXCLUDED.last_failure_at, locked_until=EXCLUDED.locked_until"
            ),
            {
                "employee_no": employee_no,
                "source": source,
                "failure_count": failure_count,
                "last_failure_at": last_failure_at,
                "locked_until": locked_until,
            },
        )

    def evict_old_full_sessions(self, account_id: str, cap: int, now: datetime) -> list[str]:
        result = self.db.execute(
            text(
                "WITH ranked AS (SELECT id, row_number() OVER "
                "(ORDER BY created_at DESC, id DESC) AS newest_rank "
                "FROM identity.session WHERE account_id=:account_id "
                "AND kind='FULL' AND revoked_at IS NULL) "
                "UPDATE identity.session s SET revoked_at=:now, revoke_reason='SESSION_CAP' "
                "FROM ranked r WHERE s.id=r.id AND r.newest_rank>:cap RETURNING s.id"
            ),
            {"account_id": account_id, "cap": cap, "now": now},
        )
        return [str(value) for value in result.scalars()]

    def revoke_session(self, session_id: str, now: datetime, reason: str) -> str | None:
        result = self.db.execute(
            text(
                "UPDATE identity.session SET revoked_at=:now, revoke_reason=:reason "
                "WHERE id=:id AND revoked_at IS NULL RETURNING id"
            ),
            {"id": session_id, "now": now, "reason": reason},
        )
        value = result.scalar_one_or_none()
        return str(value) if value is not None else None

    def touch_session(self, session_id: str, now: datetime, expires_hint: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE identity.session SET last_seen_at=:now, expires_hint=:expires_hint "
                "WHERE id=:id AND revoked_at IS NULL"
            ),
            {"id": session_id, "now": now, "expires_hint": expires_hint},
        )

    def effective_super_admins_except(self, account_id: str) -> int:
        return int(
            self.db.execute(
                text(
                    "SELECT count(*) FROM identity.account WHERE id<>:account_id "
                    "AND is_super_admin=true AND status='ENABLED' "
                    "AND password_hash IS NOT NULL AND totp_confirmed_at IS NOT NULL"
                ),
                {"account_id": account_id},
            ).scalar_one()
        )

    def update_account_status(
        self,
        account_id: str,
        status: str,
        expected_version: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE identity.account SET status=:status, version=version+1, "
                    "updated_at=:now "
                    "WHERE id=:account_id AND version=:expected_version RETURNING *"
                ),
                {
                    "account_id": account_id,
                    "status": status,
                    "expected_version": expected_version,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )
