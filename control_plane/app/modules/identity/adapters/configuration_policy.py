import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from control_plane.app.modules.identity.domain.configuration_policy import (
    OwnedPolicyDraft,
    OwnedPolicyKey,
    OwnedPolicySnapshot,
    OwnedPublishedPolicyVersion,
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

    def active_snapshot(
        self,
        namespace: str,
        *,
        for_update: bool = False,
    ) -> OwnedPolicySnapshot | None:
        if for_update:
            pointer = (
                self.db.execute(
                    text(
                        "SELECT namespace, scope, version FROM identity.active_pointer "
                        "WHERE namespace=:namespace AND scope='PLATFORM' FOR UPDATE"
                    ),
                    {"namespace": namespace},
                )
                .mappings()
                .one_or_none()
            )
            if pointer is None:
                return None
            row = (
                self.db.execute(
                    text(
                        "SELECT namespace, scope, version, schema_revision, snapshot_hash, "
                        "snapshot FROM identity.version WHERE namespace=:namespace "
                        "AND scope=:scope AND version=:version"
                    ),
                    dict(pointer),
                )
                .mappings()
                .one_or_none()
            )
        else:
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

    def version_snapshot(
        self,
        namespace: str,
        scope: str,
        version: int,
    ) -> OwnedPolicySnapshot | None:
        row = (
            self.db.execute(
                text(
                    "SELECT namespace, scope, version, schema_revision, snapshot_hash, snapshot "
                    "FROM identity.version WHERE namespace=:namespace AND scope=:scope "
                    "AND version=:version"
                ),
                {"namespace": namespace, "scope": scope, "version": version},
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

    def list_versions(
        self,
        namespace: str,
        scope: str,
        *,
        before_version: int | None,
        limit: int,
    ) -> list[OwnedPublishedPolicyVersion]:
        before_clause = " AND version < :before_version" if before_version is not None else ""
        rows = self.db.execute(
            text(
                "SELECT namespace, scope, version, snapshot, snapshot_hash, published_by, "
                "reason, published_at, activated_at, schema_revision FROM identity.version "
                "WHERE namespace=:namespace AND scope=:scope"
                + before_clause
                + " ORDER BY version DESC LIMIT :limit"
            ),
            {
                "namespace": namespace,
                "scope": scope,
                "before_version": before_version,
                "limit": limit,
            },
        ).mappings()
        return [OwnedPublishedPolicyVersion.model_validate(row) for row in rows]

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
                    "stale, last_meaningful_activity_at, archived_at, schema_revision, "
                    "content_hash, "
                    "rollback_from_version"
                    ") VALUES ("
                    ":id, :namespace, :scope, CAST(:content AS JSONB), :base_version, :owner_id, "
                    "1, 'DRAFT', false, :now, NULL, :schema_revision, :content_hash, "
                    ":rollback_from_version) RETURNING *"
                ),
                {
                    **values,
                    "content": json.dumps(values["content"], separators=(",", ":")),
                    "rollback_from_version": values.get("rollback_from_version"),
                },
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

    def publish_version(self, **values: Any) -> OwnedPublishedPolicyVersion | None:
        inserted = (
            self.db.execute(
                text(
                    "INSERT INTO identity.version ("
                    "namespace, scope, version, snapshot, changeset, published_by, reason, "
                    "published_at, activated_at, schema_revision, snapshot_hash, "
                    "validation_evidence, dependency_versions, preview_evidence"
                    ") VALUES ("
                    ":namespace, :scope, :version, CAST(:snapshot AS JSONB), "
                    "CAST(:changeset AS JSONB), :published_by, :reason, :now, :now, "
                    ":schema_revision, :snapshot_hash, CAST(:validation AS JSONB), "
                    "CAST(:dependencies AS JSONB), CAST(:preview AS JSONB)) "
                    "RETURNING namespace, scope, version, snapshot, snapshot_hash, "
                    "published_by, reason, published_at, activated_at, schema_revision"
                ),
                {
                    **values,
                    "snapshot": json.dumps(values["snapshot"], separators=(",", ":")),
                    "changeset": json.dumps(values["changeset"], separators=(",", ":")),
                    "validation": json.dumps(values["validation"], separators=(",", ":")),
                    "dependencies": json.dumps(values["dependencies"], separators=(",", ":")),
                    "preview": json.dumps(values["preview"], separators=(",", ":")),
                },
            )
            .mappings()
            .one()
        )
        pointer = self.db.execute(
            text(
                "UPDATE identity.active_pointer SET version=:version "
                "WHERE namespace=:namespace AND scope=:scope AND version=:base_version"
            ),
            values,
        )
        if pointer.rowcount != 1:
            return None
        self.db.execute(
            text(
                "UPDATE identity.draft SET stale=true, validation_evidence=NULL, "
                "validation_content_hash=NULL, validation_schema_revision=NULL, "
                "validation_base_version=NULL, validation_dependency_versions=NULL, "
                "preview_evidence=NULL, preview_content_hash=NULL, "
                "preview_schema_revision=NULL, preview_base_version=NULL, "
                "preview_dependency_versions=NULL "
                "WHERE namespace=:namespace AND scope=:scope AND status='DRAFT' "
                "AND id<>CAST(:source_draft_id AS UUID) AND base_version<:version"
            ),
            values,
        )
        self.db.execute(
            text(
                "INSERT INTO identity.configuration_outbox ("
                "id, namespace, scope, event_type, aggregate_id, payload, occurred_at"
                ") VALUES ("
                ":outbox_id, :namespace, :scope, 'POLICY_PUBLISHED', :aggregate_id, "
                "CAST(:outbox_payload AS JSONB), :now)"
            ),
            {
                **values,
                "outbox_payload": json.dumps(values["outbox_payload"], separators=(",", ":")),
            },
        )
        row = dict(inserted)
        row["snapshot"] = dict(row["snapshot"])
        return OwnedPublishedPolicyVersion.model_validate(row)

    def archive_candidates(
        self,
        namespace: str,
        scope: str,
        *,
        cutoff: datetime,
        limit: int,
    ) -> list[OwnedPolicyDraft]:
        rows = self.db.execute(
            text(
                "SELECT * FROM identity.draft WHERE namespace=:namespace AND scope=:scope "
                "AND status='DRAFT' AND last_meaningful_activity_at<=:cutoff "
                "ORDER BY id LIMIT :limit"
            ),
            {
                "namespace": namespace,
                "scope": scope,
                "cutoff": cutoff,
                "limit": limit,
            },
        ).mappings()
        return [self._draft(row) for row in rows]

    def archive_draft(self, **values: Any) -> bool:
        archived = self.db.execute(
            text(
                "UPDATE identity.draft SET status='ARCHIVED', archived_at=:archived_at, "
                "validation_evidence=NULL, validation_content_hash=NULL, "
                "validation_schema_revision=NULL, validation_base_version=NULL, "
                "validation_dependency_versions=NULL, preview_evidence=NULL, "
                "preview_content_hash=NULL, preview_schema_revision=NULL, "
                "preview_base_version=NULL, preview_dependency_versions=NULL "
                "WHERE id=CAST(:draft_id AS UUID) AND namespace=:namespace AND scope=:scope "
                "AND status='DRAFT' AND revision=:expected_revision "
                "AND owner_id=:expected_owner_id "
                "AND last_meaningful_activity_at=:expected_activity RETURNING id"
            ),
            values,
        ).scalar_one_or_none()
        if archived is None:
            return False
        self.db.execute(
            text(
                "INSERT INTO identity.configuration_outbox ("
                "id, namespace, scope, event_type, aggregate_id, payload, occurred_at"
                ") VALUES ("
                ":outbox_id, :namespace, :scope, 'DRAFT_ARCHIVED', :aggregate_id, "
                "CAST(:outbox_payload AS JSONB), :archived_at)"
            ),
            {
                **values,
                "outbox_payload": json.dumps(values["outbox_payload"], separators=(",", ":")),
            },
        )
        return True

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
                    "validation_base_version=NULL, validation_dependency_versions=NULL, "
                    "preview_evidence=NULL, preview_content_hash=NULL, "
                    "preview_schema_revision=NULL, preview_base_version=NULL, "
                    "preview_dependency_versions=NULL "
                    "WHERE id=:id AND revision=:expected_revision AND status='DRAFT' "
                    "RETURNING *"
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

    def save_preview(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
    ) -> OwnedPolicyDraft | None:
        row = (
            self.db.execute(
                text(
                    "UPDATE identity.draft SET preview_evidence=CAST(:evidence AS JSONB), "
                    "preview_content_hash=content_hash, "
                    "preview_schema_revision=schema_revision, "
                    "preview_base_version=base_version, "
                    "preview_dependency_versions=CAST(:dependencies AS JSONB) "
                    "WHERE id=:id AND revision=:expected_revision AND status='DRAFT' "
                    "RETURNING *"
                ),
                {
                    "id": draft_id,
                    "expected_revision": expected_revision,
                    "evidence": json.dumps(evidence, separators=(",", ":")),
                    "dependencies": json.dumps(dependency_versions, separators=(",", ":")),
                },
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._draft(row)
