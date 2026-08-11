import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from control_plane.app.modules.identity.domain.configuration_policy import (
    OwnedPolicyDraft,
    OwnedPolicyKey,
    OwnedPolicySnapshot,
)


class SqlAlchemyIdentityPolicyOwnerRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def claim_configuration_idempotency(self, **values: Any) -> bool:
        result = self.db.execute(
            text(
                "INSERT INTO identity.configuration_idempotency_record "
                "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                "created_at, updated_at) VALUES "
                "(:id, :actor, :operation, :idempotency_key, :request_fingerprint, "
                "'IN_PROGRESS', :now, :now) "
                "ON CONFLICT (actor, operation, idempotency_key) DO NOTHING RETURNING id"
            ),
            values,
        )
        return result.scalar_one_or_none() is not None

    def configuration_idempotency_by_scope(
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
                    "SELECT * FROM identity.configuration_idempotency_record "
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

    def complete_configuration_idempotency(
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
                "UPDATE identity.configuration_idempotency_record SET state='COMPLETED', "
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

    def catalog(self, namespace: str) -> list[OwnedPolicyKey]:
        rows = self.db.execute(
            text(
                "SELECT key, namespace, value_type, unit, default_value, min_value, "
                "max_value, enum_values, effect_semantics, schema_revision "
                "FROM identity.policy_key WHERE namespace=:namespace ORDER BY key"
            ),
            {"namespace": namespace},
        ).mappings()
        return [OwnedPolicyKey.model_validate(row) for row in rows]

    def active_snapshot(self, namespace: str) -> OwnedPolicySnapshot | None:
        row = (
            self.db.execute(
                text(
                    "SELECT p.namespace, p.scope, p.version, v.schema_revision, "
                    "v.snapshot_hash, v.snapshot FROM identity.active_pointer p "
                    "JOIN identity.version v USING (namespace, scope, version) "
                    "WHERE p.namespace=:namespace AND p.scope='PLATFORM'"
                ),
                {"namespace": namespace},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return OwnedPolicySnapshot(
            namespace=str(row["namespace"]),
            scope=str(row["scope"]),
            version=int(row["version"]),
            schema_revision=int(row["schema_revision"]),
            snapshot_hash=str(row["snapshot_hash"]),
            values=dict(row["snapshot"]),
        )

    @staticmethod
    def _draft(row: Any) -> OwnedPolicyDraft:
        values = dict(row)
        values["id"] = str(values["id"])
        return OwnedPolicyDraft.model_validate(values)

    def insert_draft(self, **values: Any) -> OwnedPolicyDraft:
        row = (
            self.db.execute(
                text(
                    "INSERT INTO identity.draft ("
                    "id, namespace, scope, content, base_version, owner_id, revision, status, "
                    "stale, last_meaningful_activity_at, archived_at, schema_revision, content_hash"
                    ") VALUES ("
                    ":id, :namespace, :scope, CAST(:content AS JSONB), :base_version, :owner_id, "
                    "1, 'DRAFT', false, :now, NULL, :schema_revision, :content_hash) RETURNING *"
                ),
                {**values, "content": json.dumps(values["content"], separators=(",", ":"))},
            )
            .mappings()
            .one()
        )
        return self._draft(row)

    def draft_by_id(
        self,
        draft_id: str,
        *,
        for_update: bool = False,
    ) -> OwnedPolicyDraft | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            self.db.execute(
                text(f"SELECT * FROM identity.draft WHERE id=:id{suffix}"),
                {"id": draft_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._draft(row)

    def update_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        content: dict[str, Any],
        content_hash: str,
        stale: bool,
        now: datetime,
    ) -> OwnedPolicyDraft | None:
        row = (
            self.db.execute(
                text(
                    "UPDATE identity.draft SET content=CAST(:content AS JSONB), "
                    "content_hash=:content_hash, revision=revision+1, stale=:stale, "
                    "last_meaningful_activity_at=:now, validation_evidence=NULL, "
                    "validation_content_hash=NULL, validation_schema_revision=NULL, "
                    "validation_base_version=NULL, validation_dependency_versions=NULL "
                    "WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": draft_id,
                    "expected_revision": expected_revision,
                    "content": json.dumps(content, separators=(",", ":")),
                    "content_hash": content_hash,
                    "stale": stale,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._draft(row)

    def save_validation(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
        now: datetime,
    ) -> OwnedPolicyDraft | None:
        row = (
            self.db.execute(
                text(
                    "UPDATE identity.draft SET revision=revision+1, "
                    "last_meaningful_activity_at=:now, "
                    "validation_evidence=CAST(:evidence AS JSONB), "
                    "validation_content_hash=content_hash, "
                    "validation_schema_revision=schema_revision, "
                    "validation_base_version=base_version, "
                    "validation_dependency_versions=CAST(:dependencies AS JSONB) "
                    "WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": draft_id,
                    "expected_revision": expected_revision,
                    "now": now,
                    "evidence": json.dumps(evidence, separators=(",", ":")),
                    "dependencies": json.dumps(dependency_versions, separators=(",", ":")),
                },
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._draft(row)
