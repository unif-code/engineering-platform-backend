import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from control_plane.app.modules.authorization.ports.repository import PrincipalSettlement


class SqlAlchemyAuthorizationRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def claim_idempotency(self, **values: Any) -> bool:
        result = self.db.execute(
            text(
                'INSERT INTO "authorization".idempotency_record '
                "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                "created_at, updated_at) VALUES "
                "(:id, :actor, :operation, :idempotency_key, :request_fingerprint, "
                "'IN_PROGRESS', :now, :now) ON CONFLICT "
                "(actor, operation, idempotency_key) DO NOTHING RETURNING id"
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
                    'SELECT * FROM "authorization".idempotency_record '
                    "WHERE actor=:actor AND operation=:operation "
                    f"AND idempotency_key=:idempotency_key{suffix}"
                ),
                {"actor": actor, "operation": operation, "idempotency_key": idempotency_key},
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
                "UPDATE \"authorization\".idempotency_record SET state='COMPLETED', "
                "http_status=:http_status, result_metadata=CAST(:metadata AS JSONB), "
                "sealed_response=:sealed_response, completed_at=:now, updated_at=:now "
                "WHERE id=:id AND state='IN_PROGRESS'"
            ),
            {
                "id": record_id,
                "http_status": http_status,
                "metadata": json.dumps(result_metadata, separators=(",", ":")),
                "sealed_response": sealed_response,
                "now": now,
            },
        )
        return result.rowcount == 1

    def insert_grant(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    'INSERT INTO "authorization"."grant" '
                    "(id, principal_id, capability, scope_type, scope_id, source, "
                    "valid_from, valid_to, status, version, created_at, updated_at) VALUES "
                    "(:id, :principal_id, :capability, :scope_type, :scope_id, :source, "
                    ":valid_from, :valid_to, 'ACTIVE', 1, :now, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def grant_by_id(self, grant_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f'SELECT * FROM "authorization"."grant" WHERE id=:id{suffix}'),
                {"id": grant_id},
            )
            .mappings()
            .one_or_none()
        )

    def list_grants(self) -> list[Any]:
        return list(
            self.db.execute(
                text('SELECT * FROM "authorization"."grant" ORDER BY created_at, id')
            ).mappings()
        )

    def effective_grants(
        self,
        *,
        principal_id: str,
        capability: str | None,
        scope_type: str | None,
        scope_id: str | None,
        now: datetime,
    ) -> list[Any]:
        conditions = [
            "principal_id=:principal_id",
            "status='ACTIVE'",
            "(valid_from IS NULL OR valid_from<=:now)",
            "(valid_to IS NULL OR :now<valid_to)",
        ]
        if capability is not None:
            conditions.append("capability=:capability")
        if scope_type is not None:
            conditions.append("scope_type=:scope_type")
            conditions.append("scope_id IS NOT DISTINCT FROM :scope_id")
        return list(
            self.db.execute(
                text(
                    'SELECT * FROM "authorization"."grant" WHERE '
                    + " AND ".join(conditions)
                    + " ORDER BY capability, scope_type, scope_id, id"
                ),
                {
                    "principal_id": principal_id,
                    "capability": capability,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "now": now,
                },
            ).mappings()
        )

    def revoke_grant(
        self,
        *,
        grant_id: str,
        expected_version: int,
        actor_id: str,
        reason: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    'UPDATE "authorization"."grant" SET status=\'REVOKED\', '
                    "version=version+1, updated_at=:now, revoked_at=:now, "
                    "revoked_by=:actor_id, revoke_reason=:reason "
                    "WHERE id=:grant_id AND status='ACTIVE' AND version=:expected_version "
                    "RETURNING *"
                ),
                {
                    "grant_id": grant_id,
                    "expected_version": expected_version,
                    "actor_id": actor_id,
                    "reason": reason,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def bump_principal_version(self, account_id: str, now: datetime) -> Any:
        return (
            self.db.execute(
                text(
                    'INSERT INTO "authorization".principal_version '
                    "(account_id, version, fence_generation, updated_at) "
                    "VALUES (:account_id, 2, 0, :now) ON CONFLICT (account_id) "
                    'DO UPDATE SET version="authorization".principal_version.version+1, '
                    "updated_at=EXCLUDED.updated_at RETURNING *"
                ),
                {"account_id": account_id, "now": now},
            )
            .mappings()
            .one()
        )

    def principal_version(self, account_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    'SELECT * FROM "authorization".principal_version '
                    f"WHERE account_id=:account_id{suffix}"
                ),
                {"account_id": account_id},
            )
            .mappings()
            .one_or_none()
        )

    def mark_fence(self, account_id: str, reason: str, now: datetime) -> int:
        return int(
            self.db.execute(
                text(
                    'INSERT INTO "authorization".principal_version '
                    "(account_id, version, fence_generation, dirty_generation, "
                    "dirty_reason, updated_at) VALUES (:account_id, 1, 1, 1, :reason, :now) "
                    "ON CONFLICT (account_id) DO UPDATE SET "
                    'fence_generation="authorization".principal_version.fence_generation+1, '
                    'dirty_generation="authorization".principal_version.fence_generation+1, '
                    "dirty_reason=EXCLUDED.dirty_reason, updated_at=EXCLUDED.updated_at "
                    "RETURNING dirty_generation"
                ),
                {"account_id": account_id, "reason": reason, "now": now},
            ).scalar_one()
        )

    def clear_fence(self, account_id: str, generation: int, now: datetime) -> bool:
        result = self.db.execute(
            text(
                'UPDATE "authorization".principal_version SET dirty_generation=NULL, '
                "dirty_reason=NULL, updated_at=:now WHERE account_id=:account_id "
                "AND dirty_generation=:generation"
            ),
            {"account_id": account_id, "generation": generation, "now": now},
        )
        return result.rowcount == 1

    def converge_fence(self, account_id: str, generation: int, now: datetime) -> Any:
        return (
            self.db.execute(
                text(
                    'UPDATE "authorization".principal_version SET version=version+1, '
                    "dirty_generation=NULL, dirty_reason=NULL, updated_at=:now "
                    "WHERE account_id=:account_id AND dirty_generation=:generation "
                    "RETURNING *"
                ),
                {"account_id": account_id, "generation": generation, "now": now},
            )
            .mappings()
            .one_or_none()
        )

    def principal_ids(self) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text(
                    'SELECT account_id FROM "authorization".principal_version '
                    'UNION SELECT principal_id FROM "authorization"."grant" '
                    "ORDER BY 1"
                )
            ).scalars()
        ]

    def lock_convergence_source(
        self,
        source_module: str,
        actor: str,
        operation: str,
        idempotency_key: str,
        idempotency_claim_id: str | None,
    ) -> None:
        source_identity = "\x1f".join(
            (
                source_module,
                idempotency_claim_id or actor,
                "claim" if idempotency_claim_id is not None else operation,
                idempotency_claim_id or idempotency_key,
            )
        )
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": source_identity},
        )

    def convergence_work_by_source(
        self,
        source_module: str,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        idempotency_claim_id: str | None = None,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        if idempotency_claim_id is not None:
            where = (
                "source_module=:source_module "
                "AND idempotency_claim_id=CAST(:idempotency_claim_id AS UUID)"
            )
        else:
            where = (
                "source_module=:source_module AND actor=:actor "
                "AND operation=:operation AND idempotency_key=:idempotency_key"
            )
        return (
            self.db.execute(
                text(
                    'SELECT * FROM "authorization".convergence_work '
                    f"WHERE {where} ORDER BY created_at DESC, id DESC LIMIT 1{suffix}"
                ),
                {
                    "source_module": source_module,
                    "actor": actor,
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                    "idempotency_claim_id": idempotency_claim_id,
                },
            )
            .mappings()
            .one_or_none()
        )

    def convergence_work_by_id(
        self,
        work_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text('SELECT * FROM "authorization".convergence_work WHERE id=:id' + suffix),
                {"id": work_id},
            )
            .mappings()
            .one_or_none()
        )

    def convergence_status_for_claim(
        self,
        source_module: str,
        idempotency_claim_id: str,
    ) -> str | None:
        value = self.db.execute(
            text(
                'SELECT status FROM "authorization".convergence_work '
                "WHERE source_module=:source_module "
                "AND idempotency_claim_id=CAST(:idempotency_claim_id AS UUID)"
            ),
            {
                "source_module": source_module,
                "idempotency_claim_id": idempotency_claim_id,
            },
        ).scalar_one_or_none()
        return str(value) if value is not None else None

    def insert_convergence_work(self, **values: Any) -> Any:
        parameters = {
            **values,
            "generation_map": json.dumps(values["generation_map"], separators=(",", ":")),
            "affected_account_ids": json.dumps(
                values["affected_account_ids"], separators=(",", ":")
            ),
            "affected_workspace_ids": json.dumps(
                values["affected_workspace_ids"], separators=(",", ":")
            ),
        }
        return (
            self.db.execute(
                text(
                    'INSERT INTO "authorization".convergence_work '
                    "(id, source_module, actor, operation, idempotency_key, reason, "
                    "source_transaction_id, "
                    "idempotency_claim_id, request_fingerprint, "
                    "generation_map, affected_account_ids, affected_workspace_ids, "
                    "recompute_membership, status, phase, created_at, updated_at) VALUES "
                    "(:id, :source_module, :actor, :operation, :idempotency_key, :reason, "
                    ":source_transaction_id, "
                    "CAST(:idempotency_claim_id AS UUID), :request_fingerprint, "
                    "CAST(:generation_map AS JSONB), CAST(:affected_account_ids AS JSONB), "
                    "CAST(:affected_workspace_ids AS JSONB), :recompute_membership, "
                    "'PENDING', 'SOURCE_REGISTERED', :now, :now) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def insert_pending_principal(self, **values: Any) -> None:
        self.db.execute(
            text(
                'INSERT INTO "authorization".convergence_principal_pending '
                "(work_id, account_id, generation, reason, created_at) VALUES "
                "(:work_id, :account_id, :generation, :reason, :created_at)"
            ),
            values,
        )

    def source_transaction_status(self, source_transaction_id: str) -> str:
        return str(
            self.db.execute(
                text("SELECT pg_xact_status(CAST(:source_xid AS xid8))"),
                {"source_xid": source_transaction_id},
            ).scalar_one()
        )

    def update_convergence_effects(
        self,
        work_id: str,
        *,
        affected_account_ids: list[str],
        affected_workspace_ids: list[str],
        recompute_membership: bool,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    'UPDATE "authorization".convergence_work SET '
                    "affected_account_ids=CAST(:accounts AS JSONB), "
                    "affected_workspace_ids=CAST(:workspaces AS JSONB), "
                    "recompute_membership=:recompute_membership, updated_at=:now "
                    "WHERE id=:id AND status='PENDING' RETURNING *"
                ),
                {
                    "id": work_id,
                    "accounts": json.dumps(affected_account_ids, separators=(",", ":")),
                    "workspaces": json.dumps(affected_workspace_ids, separators=(",", ":")),
                    "recompute_membership": recompute_membership,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def update_convergence_phase(
        self,
        work_id: str,
        phase: str,
        now: datetime,
    ) -> None:
        self.db.execute(
            text(
                'UPDATE "authorization".convergence_work '
                "SET phase=:phase, updated_at=:now WHERE id=:id AND status='PENDING'"
            ),
            {"id": work_id, "phase": phase, "now": now},
        )

    def complete_convergence_work(self, work_id: str, now: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE \"authorization\".convergence_work SET status='COMPLETED', "
                "phase='DONE', completed_at=:now, updated_at=:now "
                "WHERE id=:id AND status='PENDING'"
            ),
            {"id": work_id, "now": now},
        )

    def cancel_convergence_work(self, work_id: str, now: datetime) -> None:
        self.db.execute(
            text(
                "UPDATE \"authorization\".convergence_work SET status='CANCELLED', "
                "phase='CANCELLED', cancelled_at=:now, updated_at=:now "
                "WHERE id=:id AND status='PENDING'"
            ),
            {"id": work_id, "now": now},
        )

    def settle_pending_principal(
        self,
        work_id: str,
        account_id: str,
        *,
        bump_version: bool,
        now: datetime,
    ) -> Any:
        before = self.db.execute(
            text(
                'SELECT version FROM "authorization".principal_version '
                "WHERE account_id=:account_id FOR UPDATE"
            ),
            {"account_id": account_id},
        ).scalar_one()
        association = self.db.execute(
            text(
                'DELETE FROM "authorization".convergence_principal_pending '
                "WHERE work_id=:work_id AND account_id=:account_id "
                "RETURNING generation"
            ),
            {"work_id": work_id, "account_id": account_id},
        ).scalar_one_or_none()
        if association is None:
            return None
        remaining = (
            self.db.execute(
                text(
                    "SELECT generation, reason FROM "
                    '"authorization".convergence_principal_pending '
                    "WHERE account_id=:account_id "
                    "ORDER BY generation DESC, work_id LIMIT 1"
                ),
                {"account_id": account_id},
            )
            .mappings()
            .one_or_none()
        )
        updated = (
            self.db.execute(
                text(
                    'UPDATE "authorization".principal_version SET '
                    "version=version+:version_increment, "
                    "dirty_generation=:dirty_generation, dirty_reason=:dirty_reason, "
                    "updated_at=:now WHERE account_id=:account_id RETURNING *"
                ),
                {
                    "account_id": account_id,
                    "version_increment": 1 if bump_version else 0,
                    "dirty_generation": (
                        int(remaining["generation"]) if remaining is not None else None
                    ),
                    "dirty_reason": str(remaining["reason"]) if remaining is not None else None,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        return PrincipalSettlement(
            before_version=int(before),
            after_version=int(updated["version"]),
        )

    def pending_convergence_for_account(self, account_id: str) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text(
                    'SELECT work.id FROM "authorization".convergence_work AS work '
                    'JOIN "authorization".convergence_principal_pending AS pending '
                    "ON pending.work_id=work.id "
                    "WHERE work.status='PENDING' AND pending.account_id=:account_id "
                    "ORDER BY work.created_at, work.id"
                ),
                {"account_id": account_id},
            ).scalars()
        ]

    def pending_convergence_work_ids(self) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text(
                    'SELECT id FROM "authorization".convergence_work '
                    "WHERE status='PENDING' ORDER BY created_at, id"
                )
            ).scalars()
        ]

    def route_registry(self) -> list[Any]:
        return list(
            self.db.execute(
                text('SELECT * FROM "authorization".route_registry ORDER BY sort, route_key')
            ).mappings()
        )
