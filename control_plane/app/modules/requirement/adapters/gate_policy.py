"""Requirement-only policy SQL and typed catalog adapter."""

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

from control_plane.app.modules.configuration import (
    Draft,
    InvalidPolicyValue,
    PolicyKey,
    PolicySnapshot,
    PolicySnapshotUnavailable,
    PreviewItem,
    PublishedVersion,
    ValidationIssue,
)
from control_plane.app.modules.identity import ConsumedReauthReceipt
from control_plane.app.modules.requirement.domain.gate_policy import (
    ACCEPTANCE_KEY,
    ARCHIVE_KEY,
    FORMAL_KEY,
    MAX_ARCHIVE_DAYS,
    NAMESPACE,
    GatePolicy,
    content_hash,
)


class SqlAlchemyGatePolicyRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def catalog(self, namespace: str) -> list[PolicyKey]:
        if namespace != NAMESPACE:
            raise PolicySnapshotUnavailable("Unsupported policy namespace")
        return [
            PolicyKey(
                key=key,
                namespace=NAMESPACE,
                value_type="INTEGER" if key == ARCHIVE_KEY else "STRING_SET",
                unit="DAYS" if key == ARCHIVE_KEY else None,
                default_value=30 if key == ARCHIVE_KEY else [],
                min_value=1 if key == ARCHIVE_KEY else None,
                max_value=MAX_ARCHIVE_DAYS if key == ARCHIVE_KEY else None,
                enum_values=None if key == ARCHIVE_KEY else ["code.change"],
                effect_semantics="NEXT_SCHEDULE" if key == ARCHIVE_KEY else "NEW_OBJECT",
                schema_revision=1,
            )
            for key in (ACCEPTANCE_KEY, FORMAL_KEY, ARCHIVE_KEY)
        ]

    @staticmethod
    def _snapshot(row: Any) -> PolicySnapshot:
        try:
            if row is None:
                raise ValueError("Missing policy")
            policy = GatePolicy.parse(
                dict(row["snapshot"]),
                namespace=row["namespace"],
                scope=row["scope"],
                schema_revision=row["schema_revision"],
            )
            if content_hash(policy.values()) != row["snapshot_hash"] or row["version"] < 1:
                raise ValueError("Invalid policy hash")
            return PolicySnapshot(
                namespace=row["namespace"],
                scope=row["scope"],
                version=row["version"],
                schema_revision=row["schema_revision"],
                snapshot_hash=row["snapshot_hash"],
                values=policy.values(),
            )
        except (ValueError, TypeError, KeyError):
            raise PolicySnapshotUnavailable("Effective Gate policy unavailable") from None

    def active_snapshot(self, namespace: str, *, for_update: bool = False) -> PolicySnapshot:
        self.catalog(namespace)
        suffix = " FOR UPDATE OF p" if for_update else ""
        row = (
            self.db.execute(
                text(
                    "SELECT v.* FROM requirement.gate_policy_active_pointer p JOIN "
                    "requirement.gate_policy_version v USING(namespace,scope,version) WHERE "
                    "p.namespace=:namespace AND p.scope='PLATFORM'" + suffix
                ),
                {"namespace": namespace},
            )
            .mappings()
            .one_or_none()
        )
        return self._snapshot(row)

    def active_archive_settings(self, namespace: str) -> tuple[PolicySnapshot, timedelta]:
        # Publication and archival must acquire the pointer before any draft.
        # Read the snapshot in a new statement after waiting for this lock, so
        # its version and interval reflect a publication that just committed.
        self.db.execute(
            text(
                "SELECT namespace FROM requirement.gate_policy_active_pointer "
                "WHERE namespace=:namespace AND scope='PLATFORM' FOR UPDATE"
            ),
            {"namespace": namespace},
        ).scalar_one_or_none()
        snapshot = self.active_snapshot(namespace)
        return snapshot, timedelta(days=snapshot.values[ARCHIVE_KEY])

    def version_snapshot(self, namespace: str, scope: str, version: int) -> PolicySnapshot | None:
        self.catalog(namespace)
        row = (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.gate_policy_version WHERE namespace=:namespace AND "
                    "scope=:scope AND version=:version"
                ),
                {"namespace": namespace, "scope": scope, "version": version},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._snapshot(row)

    def list_versions(
        self, namespace: str, scope: str, *, before_version: int | None, limit: int
    ) -> list[PublishedVersion]:
        self.catalog(namespace)
        rows = self.db.execute(
            text(
                "SELECT * FROM requirement.gate_policy_version WHERE namespace=:namespace AND "
                "scope=:scope AND (CAST(:before AS BIGINT) IS NULL OR version<:before) ORDER BY "
                "version DESC LIMIT :limit"
            ),
            {"namespace": namespace, "scope": scope, "before": before_version, "limit": limit},
        ).mappings()
        result = []
        for row in rows:
            self._snapshot(row)
            result.append(PublishedVersion.model_validate(dict(row)))
        return result

    def validate_candidate(
        self, namespace: str, *, schema_revision: int, values: dict[str, Any]
    ) -> list[ValidationIssue]:
        try:
            GatePolicy.parse(
                values, namespace=namespace, scope="PLATFORM", schema_revision=schema_revision
            )
        except ValueError:
            return [
                ValidationIssue(
                    code="INVALID_GATE_POLICY", key=NAMESPACE, message="Invalid Gate policy"
                )
            ]
        return []

    @staticmethod
    def _draft(row: Any) -> Draft:
        return Draft.model_validate({**dict(row), "id": str(row["id"])})

    def create_draft(self, **values: Any) -> Draft:
        if values["scope"] != "PLATFORM" or self.validate_candidate(
            values["namespace"], schema_revision=values["schema_revision"], values=values["content"]
        ):
            raise InvalidPolicyValue("Invalid Gate policy")
        self._check_archive_cutoff(values["content"], values["now"])
        row = (
            self.db.execute(
                text(
                    "INSERT INTO requirement.gate_policy_draft "
                    "(id,namespace,scope,content,base_version,owner_id,revision,status,stale,la"
                    "st_meaningful_activity_at,schema_revision,content_hash,rollback_from_versi"
                    "on) "
                    "VALUES (:id,:namespace,:scope,CAST(:content AS "
                    "JSONB),:base_version,:owner_id,1,'DRAFT',false,:now,:schema_revision,:cont"
                    "ent_hash,:rollback_from_version) "
                    "RETURNING *"
                ),
                {
                    **values,
                    "content": json.dumps(values["content"]),
                    "rollback_from_version": values.get("rollback_from_version"),
                },
            )
            .mappings()
            .one()
        )
        return self._draft(row)

    def draft(self, draft_id: str, *, for_update: bool = False) -> Draft | None:
        row = (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.gate_policy_draft WHERE id=:id"
                    + (" FOR UPDATE" if for_update else "")
                ),
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
    ) -> Draft | None:
        if self.validate_candidate(NAMESPACE, schema_revision=1, values=content):
            raise InvalidPolicyValue("Invalid Gate policy")
        self._check_archive_cutoff(content, now)
        row = (
            self.db.execute(
                text(
                    "UPDATE requirement.gate_policy_draft SET content=CAST(:content AS "
                    "JSONB),content_hash=:hash,revision=revision+1,stale=:stale,last_meaningful"
                    "_activity_at=:now,validation_evidence=NULL,preview_evidence=NULL "
                    "WHERE id=:id AND revision=:revision AND status='DRAFT' RETURNING *"
                ),
                {
                    "id": draft_id,
                    "content": json.dumps(content),
                    "hash": content_hash,
                    "revision": expected_revision,
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
    ) -> Draft | None:
        row = (
            self.db.execute(
                text(
                    "UPDATE requirement.gate_policy_draft SET "
                    "revision=revision+1,last_meaningful_activity_at=:now,preview_evidence=NULL"
                    ",validation_evidence=CAST(:evidence "
                    "AS JSONB) || "
                    "jsonb_build_object('content_hash',content_hash,'schema_revision',schema_re"
                    "vision,'base_version',base_version,'dependency_versions',CAST(:dependencie"
                    "s "
                    "AS JSONB)) WHERE id=:id AND revision=:revision AND status='DRAFT' RETURNING "
                    "*"
                ),
                {
                    "id": draft_id,
                    "revision": expected_revision,
                    "now": now,
                    "evidence": json.dumps(evidence),
                    "dependencies": json.dumps(dependency_versions),
                },
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._draft(row)

    def preview_candidate(
        self, namespace: str, *, before: dict[str, Any], after: dict[str, Any]
    ) -> list[PreviewItem]:
        return [
            PreviewItem(
                key=key.key,
                before=before[key.key],
                after=after[key.key],
                effect_semantics=key.effect_semantics,
                impact="Applies to the next archival schedule"
                if key.key == ARCHIVE_KEY
                else "Additional capability required for new Gates",
            )
            for key in self.catalog(namespace)
            if before[key.key] != after[key.key]
        ]

    def save_preview(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
    ) -> Draft | None:
        row = (
            self.db.execute(
                text(
                    "UPDATE requirement.gate_policy_draft SET preview_evidence=CAST(:evidence AS "
                    "JSONB) || "
                    "jsonb_build_object('schema_revision',schema_revision,'dependency_versions'"
                    ",CAST(:dependencies "
                    "AS JSONB)) WHERE id=:id AND revision=:revision AND status='DRAFT' RETURNING "
                    "*"
                ),
                {
                    "id": draft_id,
                    "revision": expected_revision,
                    "evidence": json.dumps(evidence),
                    "dependencies": json.dumps(dependency_versions),
                },
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._draft(row)

    def archive_candidates(
        self, namespace: str, scope: str, *, cutoff: datetime, limit: int
    ) -> list[Draft]:
        rows = self.db.execute(
            text(
                "SELECT * FROM requirement.gate_policy_draft WHERE namespace=:namespace AND "
                "scope=:scope AND status='DRAFT' AND last_meaningful_activity_at<=:cutoff ORDER "
                "BY id LIMIT :limit"
            ),
            {"namespace": namespace, "scope": scope, "cutoff": cutoff, "limit": limit},
        ).mappings()
        return [self._draft(row) for row in rows]

    def archive_draft(self, **values: Any) -> bool:
        row = self.db.execute(
            text(
                "UPDATE requirement.gate_policy_draft SET "
                "status='ARCHIVED',archived_at=:archived_at,validation_evidence=NULL,preview_ev"
                "idence=NULL "
                "WHERE id=:draft_id AND namespace=:namespace AND scope=:scope AND status='DRAFT' "
                "AND revision=:expected_revision AND owner_id=:expected_owner_id AND "
                "last_meaningful_activity_at=:expected_activity RETURNING id"
            ),
            values,
        ).scalar_one_or_none()
        if row is None:
            return False
        self.outbox(event_type="DRAFT_ARCHIVED", now=values["archived_at"], **values)
        return True

    def outbox(self, *, event_type: str, now: datetime, **values: Any) -> None:
        self.db.execute(
            text(
                "INSERT INTO requirement.gate_policy_outbox "
                "(id,namespace,scope,event_type,aggregate_id,payload,occurred_at) VALUES "
                "(:outbox_id,:namespace,:scope,:event_type,:aggregate_id,CAST(:payload AS "
                "JSONB),:now)"
            ),
            {
                **values,
                "event_type": event_type,
                "now": now,
                "payload": json.dumps(values["outbox_payload"]),
            },
        )

    def claim_idempotency(self, **values: Any) -> bool:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.gate_policy_idempotency "
                    "(id,actor,operation,idempotency_key,request_fingerprint,state,created_at,u"
                    "pdated_at) "
                    "VALUES "
                    "(:id,:actor,:operation,:idempotency_key,:request_fingerprint,'IN_PROGRESS'"
                    ",:now,:now) "
                    "ON CONFLICT(actor,operation,idempotency_key) DO NOTHING RETURNING id"
                ),
                values,
            ).scalar_one_or_none()
            is not None
        )

    def idempotency_by_scope(
        self, actor: str, operation: str, idempotency_key: str, *, for_update: bool = False
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.gate_policy_idempotency WHERE actor=:actor AND "
                    "operation=:operation AND idempotency_key=:key"
                    + (" FOR UPDATE" if for_update else "")
                ),
                {"actor": actor, "operation": operation, "key": idempotency_key},
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
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.gate_policy_idempotency SET "
                    "state='COMPLETED',http_status=:status,result_metadata=CAST(:metadata AS "
                    "JSONB),sealed_response=:response,updated_at=:now,completed_at=:now WHERE "
                    "id=:id AND state='IN_PROGRESS'"
                ),
                {
                    "id": record_id,
                    "status": http_status,
                    "metadata": json.dumps(result_metadata),
                    "response": sealed_response,
                    "now": now,
                },
            ).rowcount
            == 1
        )

    def reference_receipt(self, receipt: ConsumedReauthReceipt, *, now: datetime) -> None:
        self.db.execute(
            text(
                "INSERT INTO "
                "requirement.gate_policy_receipt(receipt_id,operation,draft_id,binding,referenc"
                "ed_at) "
                "VALUES (:id,:operation,:draft,CAST(:binding AS JSONB),:now)"
            ),
            {
                "id": receipt.receipt_id,
                "operation": receipt.binding.operation,
                "draft": receipt.binding.draft_id,
                "binding": receipt.binding.canonical_json(),
                "now": now,
            },
        )

    def publish(
        self,
        draft: Draft,
        receipt: ConsumedReauthReceipt,
        *,
        actor_id: str,
        reason: str,
        now: datetime,
        outbox_id: str,
    ) -> PublishedVersion:
        self._check_archive_cutoff(draft.content, now)
        version = draft.base_version + 1
        self.reference_receipt(receipt, now=now)
        row = (
            self.db.execute(
                text(
                    "INSERT INTO "
                    "requirement.gate_policy_version(namespace,scope,version,snapshot,snapshot_"
                    "hash,schema_revision,published_by,reason,published_at,activated_at,source_"
                    "draft_id,receipt_id,validation_evidence,preview_evidence,dependency_versio"
                    "ns) "
                    "VALUES (:namespace,:scope,:version,CAST(:content AS "
                    "JSONB),:hash,:schema,:actor,:reason,:now,:now,:draft,:receipt,CAST(:valida"
                    "tion "
                    "AS JSONB),CAST(:preview AS JSONB),'{}') RETURNING *"
                ),
                {
                    "namespace": draft.namespace,
                    "scope": draft.scope,
                    "version": version,
                    "content": json.dumps(draft.content),
                    "hash": draft.content_hash,
                    "schema": draft.schema_revision,
                    "actor": actor_id,
                    "reason": reason,
                    "now": now,
                    "draft": draft.id,
                    "receipt": receipt.receipt_id,
                    "validation": json.dumps(draft.validation_evidence),
                    "preview": json.dumps(draft.preview_evidence),
                },
            )
            .mappings()
            .one()
        )
        changed = self.db.execute(
            text(
                "UPDATE requirement.gate_policy_active_pointer SET version=:version WHERE "
                "namespace=:namespace AND scope=:scope AND version=:base"
            ),
            {
                "namespace": draft.namespace,
                "scope": draft.scope,
                "version": version,
                "base": draft.base_version,
            },
        ).rowcount
        if changed != 1:
            raise PolicySnapshotUnavailable("Active policy changed")
        self.db.execute(
            text(
                "UPDATE requirement.gate_policy_draft SET "
                "stale=true,validation_evidence=NULL,preview_evidence=NULL WHERE "
                "namespace=:namespace AND status='DRAFT' AND base_version<:version"
            ),
            {"namespace": draft.namespace, "version": version},
        )
        self.outbox(
            namespace=draft.namespace,
            scope=draft.scope,
            event_type="POLICY_PUBLISHED",
            now=now,
            outbox_id=outbox_id,
            aggregate_id=f"{draft.namespace}:PLATFORM:{version}",
            outbox_payload={
                "namespace": draft.namespace,
                "scope": draft.scope,
                "version": version,
                "snapshotHash": draft.content_hash,
            },
        )
        return PublishedVersion.model_validate(dict(row))

    @staticmethod
    def _check_archive_cutoff(content: dict[str, Any], now: datetime) -> None:
        try:
            GatePolicy.parse(
                content, namespace=NAMESPACE, scope="PLATFORM", schema_revision=1
            ).archive_cutoff(now)
        except ValueError:
            raise InvalidPolicyValue("Invalid draft archive interval") from None
