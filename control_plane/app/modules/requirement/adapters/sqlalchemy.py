import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from control_plane.app.modules.requirement.domain import FormalDeliveryConflict


class SqlAlchemyRequirementRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def claim_idempotency(self, **values: Any) -> bool:
        result = self.db.execute(
            text(
                "INSERT INTO requirement.idempotency_record "
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
                    "SELECT * FROM requirement.idempotency_record "
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

    def completed_idempotency_by_fingerprint(
        self,
        actor: str,
        operation: str,
        request_fingerprint: str,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM requirement.idempotency_record "
                    "WHERE actor=:actor AND operation=:operation "
                    "AND request_fingerprint=:request_fingerprint "
                    "AND state='COMPLETED' ORDER BY created_at, id"
                ),
                {
                    "actor": actor,
                    "operation": operation,
                    "request_fingerprint": request_fingerprint,
                },
            ).mappings()
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
                "UPDATE requirement.idempotency_record SET state='COMPLETED', "
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

    def insert_requirement(self, **values: Any) -> Any:
        route_snapshot = values.get(
            "route_snapshot",
            {
                "requirementType": values["type"],
                "requiredCapabilities": ["code.change"],
                "version": values["route_snapshot_version"],
            },
        )
        parameters = {
            **values,
            "acceptance_criteria": json.dumps(
                values["acceptance_criteria"],
                separators=(",", ":"),
            ),
            "route_snapshot": json.dumps(route_snapshot, separators=(",", ":")),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.requirement "
                    "(id, workspace_id, type, title, description, acceptance_criteria, "
                    "acceptance_criteria_version, acceptance_criteria_hash, "
                    "created_by, initial_repository_id, route_snapshot_version, "
                    "route_snapshot_hash, route_snapshot, state, record_state, "
                    "requirement_version, "
                    "required_work_item_set_version, required_work_item_set_hash, revision, "
                    "created_at, updated_at) VALUES "
                    "(:id, :workspace_id, :type, :title, :description, "
                    "CAST(:acceptance_criteria AS JSONB), :acceptance_criteria_version, "
                    ":acceptance_criteria_hash, :created_by, :initial_repository_id, "
                    ":route_snapshot_version, :route_snapshot_hash, "
                    "CAST(:route_snapshot AS JSONB), :state, :record_state, "
                    ":requirement_version, :required_work_item_set_version, "
                    ":required_work_item_set_hash, :revision, :now, :now) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def requirement_by_id(
        self,
        requirement_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.requirement WHERE id=:id{suffix}"),
                {"id": requirement_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_delivery_snapshot(self, **values: Any) -> Any:
        parameters = {
            **values,
            "work_item_ids": json.dumps(values["work_item_ids"], separators=(",", ":")),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.requirement_delivery_snapshot "
                    "(id, requirement_id, requirement_version, "
                    "required_work_item_set_version, required_work_item_set_hash, "
                    "work_item_ids, snapshot_hash, created_by, created_at) VALUES "
                    "(:id, :requirement_id, :requirement_version, "
                    ":required_work_item_set_version, :required_work_item_set_hash, "
                    "CAST(:work_item_ids AS JSONB), :snapshot_hash, :created_by, :now) "
                    "ON CONFLICT (requirement_id, snapshot_hash) DO NOTHING "
                    "RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one_or_none()
        )

    def delivery_snapshot_by_hash(
        self,
        requirement_id: str,
        snapshot_hash: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.requirement_delivery_snapshot "
                    "WHERE requirement_id=:requirement_id AND snapshot_hash=:snapshot_hash"
                ),
                {"requirement_id": requirement_id, "snapshot_hash": snapshot_hash},
            )
            .mappings()
            .one_or_none()
        )

    def delivery_snapshot_by_id(self, snapshot_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.requirement_delivery_snapshot WHERE id=:snapshot_id"
                ),
                {"snapshot_id": snapshot_id},
            )
            .mappings()
            .one_or_none()
        )

    def latest_delivery_snapshot(self, requirement_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.requirement_delivery_snapshot "
                    "WHERE requirement_id=:requirement_id "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"requirement_id": requirement_id},
            )
            .mappings()
            .one_or_none()
        )

    def integration_baseline_selection_by_id(self, selection_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.integration_baseline_selection "
                    "WHERE id=:selection_id"
                ),
                {"selection_id": selection_id},
            )
            .mappings()
            .one_or_none()
        )

    def current_integration_baseline_selection(
        self,
        requirement_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.integration_baseline_selection "
                    "WHERE requirement_id=:requirement_id AND invalidated_at IS NULL"
                    f"{suffix}"
                ),
                {"requirement_id": requirement_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_integration_baseline_selection(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.integration_baseline_selection "
                    "(id, requirement_id, delivery_snapshot_id, delivery_snapshot_hash, "
                    "integration_baseline_id, integration_baseline_hash, "
                    "evidence_requirement_version, "
                    "evidence_required_work_item_set_version, "
                    "evidence_required_work_item_set_hash, requirement_version_before, "
                    "requirement_version_after, selected_by, selected_at) VALUES "
                    "(:id, :requirement_id, :delivery_snapshot_id, :delivery_snapshot_hash, "
                    ":integration_baseline_id, :integration_baseline_hash, "
                    ":evidence_requirement_version, "
                    ":evidence_required_work_item_set_version, "
                    ":evidence_required_work_item_set_hash, :requirement_version_before, "
                    ":requirement_version_after, :selected_by, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def apply_integration_baseline_selection(
        self,
        requirement_id: str,
        *,
        selection_id: str,
        expected_revision: int,
        expected_requirement_version: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET "
                    "current_integration_baseline_selection_id=:selection_id, "
                    "current_acceptance_gate_id=NULL, state='AWAITING_ACCEPTANCE', "
                    "requirement_version=requirement_version + 1, revision=revision + 1, "
                    "updated_at=:now WHERE id=:requirement_id "
                    "AND state='VERIFYING' AND revision=:expected_revision "
                    "AND requirement_version=:expected_requirement_version "
                    "AND current_integration_baseline_selection_id IS NULL RETURNING *"
                ),
                {
                    "requirement_id": requirement_id,
                    "selection_id": selection_id,
                    "expected_revision": expected_revision,
                    "expected_requirement_version": expected_requirement_version,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_delivery_gate(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.delivery_gate "
                    "(id, gate_type, requirement_id, work_item_id, selection_id, "
                    "requirement_version, acceptance_criteria_version, "
                    "acceptance_criteria_hash, integration_baseline_id, "
                    "integration_baseline_hash, formal_merge_request_binding_id, "
                    "subject_head_sha, policy_code, policy_version, policy_snapshot_hash, "
                    "state, revision, created_at) VALUES "
                    "(:id, :gate_type, :requirement_id, :work_item_id, :selection_id, "
                    ":requirement_version, :acceptance_criteria_version, "
                    ":acceptance_criteria_hash, :integration_baseline_id, "
                    ":integration_baseline_hash, :formal_merge_request_binding_id, "
                    ":subject_head_sha, :policy_code, :policy_version, "
                    ":policy_snapshot_hash, 'OPEN', 1, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def delivery_gate_by_id(
        self,
        gate_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.delivery_gate WHERE id=:gate_id{suffix}"),
                {"gate_id": gate_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_delivery_gate_assignment(self, **values: Any) -> Any:
        parameters = {
            "revision": 1,
            **values,
            "resolution_snapshot": json.dumps(
                values["resolution_snapshot"],
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.delivery_gate_assignment "
                    "(id, gate_id, default_reviewer_id, current_reviewer_id, "
                    "resolution_snapshot, revision, assigned_at) VALUES "
                    "(:id, :gate_id, :default_reviewer_id, :current_reviewer_id, "
                    "CAST(:resolution_snapshot AS JSONB), :revision, :now) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def advance_delivery_gate_assignment(self, gate_id: str, *, expected_revision: int) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.delivery_gate SET revision=revision+1 "
                    "WHERE id=:gate_id AND revision=:revision AND state='OPEN' RETURNING *"
                ),
                {"gate_id": gate_id, "revision": expected_revision},
            )
            .mappings()
            .one_or_none()
        )

    def supersede_delivery_gate_assignment(self, assignment_id: str, *, now: datetime) -> bool:
        result = self.db.execute(
            text(
                "UPDATE requirement.delivery_gate_assignment SET superseded_at=:now "
                "WHERE id=:id AND superseded_at IS NULL"
            ),
            {"id": assignment_id, "now": now},
        )
        return result.rowcount == 1

    def current_delivery_gate_assignment(
        self,
        gate_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.delivery_gate_assignment "
                    "WHERE gate_id=:gate_id AND superseded_at IS NULL"
                    f"{suffix}"
                ),
                {"gate_id": gate_id},
            )
            .mappings()
            .one_or_none()
        )

    def set_current_acceptance_gate(
        self,
        requirement_id: str,
        *,
        selection_id: str,
        gate_id: str,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET current_acceptance_gate_id=:gate_id, "
                    "revision=revision + 1, updated_at=:now WHERE id=:requirement_id "
                    "AND state='AWAITING_ACCEPTANCE' AND revision=:expected_revision "
                    "AND current_integration_baseline_selection_id=:selection_id "
                    "AND current_acceptance_gate_id IS NULL RETURNING *"
                ),
                {
                    "requirement_id": requirement_id,
                    "selection_id": selection_id,
                    "gate_id": gate_id,
                    "expected_revision": expected_revision,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_delivery_decision(self, **values: Any) -> Any:
        parameters = {
            **values,
            "eligibility_snapshot": json.dumps(
                values["eligibility_snapshot"],
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.delivery_decision "
                    "(id, gate_id, gate_assignment_id, reviewer_id, outcome, reason, "
                    "subject_revision, requirement_version, acceptance_criteria_version, "
                    "acceptance_criteria_hash, integration_baseline_id, "
                    "integration_baseline_hash, subject_head_sha, eligibility_snapshot, "
                    "validity, decided_at) VALUES "
                    "(:id, :gate_id, :gate_assignment_id, :reviewer_id, :outcome, "
                    ":reason, :subject_revision, :requirement_version, "
                    ":acceptance_criteria_version, :acceptance_criteria_hash, "
                    ":integration_baseline_id, :integration_baseline_hash, "
                    ":subject_head_sha, CAST(:eligibility_snapshot AS JSONB), 'CURRENT', "
                    ":now) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def delivery_decision_by_gate(self, gate_id: str) -> Any:
        return (
            self.db.execute(
                text("SELECT * FROM requirement.delivery_decision WHERE gate_id=:gate_id"),
                {"gate_id": gate_id},
            )
            .mappings()
            .one_or_none()
        )

    def current_formal_delivery_projections(self, requirement_id: str) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT work_item.id AS work_item_id, to_jsonb(formal_gate) AS gate, "
                    "to_jsonb(assignment) AS assignment, to_jsonb(decision) AS decision "
                    "FROM requirement.work_item AS work_item "
                    "LEFT JOIN LATERAL ("
                    "SELECT gate.* FROM requirement.delivery_gate AS gate "
                    "WHERE gate.requirement_id=work_item.requirement_id "
                    "AND gate.work_item_id=work_item.id "
                    "AND gate.gate_type='FORMAL_MR_REVIEW' "
                    "AND gate.invalidated_at IS NULL "
                    "AND gate.formal_merge_request_binding_id="
                    "work_item.formal_merge_request_binding_id "
                    "ORDER BY gate.created_at DESC, gate.id DESC LIMIT 1"
                    ") AS formal_gate ON TRUE "
                    "LEFT JOIN LATERAL ("
                    "SELECT value.* FROM requirement.delivery_gate_assignment AS value "
                    "WHERE value.gate_id=formal_gate.id AND value.superseded_at IS NULL "
                    "ORDER BY value.assigned_at DESC, value.id DESC LIMIT 1"
                    ") AS assignment ON TRUE "
                    "LEFT JOIN requirement.delivery_decision AS decision "
                    "ON decision.gate_id=formal_gate.id "
                    "WHERE work_item.requirement_id=:requirement_id "
                    "ORDER BY work_item.created_at, work_item.id"
                ),
                {"requirement_id": requirement_id},
            ).mappings()
        )

    def list_delivery_history(
        self,
        requirement_id: str,
        *,
        before_occurred_at: datetime | None,
        before_fact_type: str | None,
        before_fact_id: str | None,
        limit: int,
    ) -> list[Any]:
        cursor_clause = ""
        if before_occurred_at is not None:
            cursor_clause = (
                "WHERE (occurred_at, fact_type, fact_id) < "
                "(CAST(:before_occurred_at AS TIMESTAMPTZ), :before_fact_type, "
                "CAST(:before_fact_id AS UUID)) "
            )
        statement = text(
            "WITH history AS ("
            "SELECT 'DELIVERY_SNAPSHOT'::TEXT AS fact_type, snapshot.created_at AS occurred_at, "
            "snapshot.id AS fact_id, to_jsonb(snapshot) AS fact "
            "FROM requirement.requirement_delivery_snapshot AS snapshot "
            "WHERE snapshot.requirement_id=:requirement_id "
            "UNION ALL "
            "SELECT 'INTEGRATION_BASELINE_SELECTION', selection.selected_at, selection.id, "
            "to_jsonb(selection) FROM requirement.integration_baseline_selection AS selection "
            "WHERE selection.requirement_id=:requirement_id "
            "UNION ALL "
            "SELECT 'DELIVERY_GATE', gate.created_at, gate.id, to_jsonb(gate) "
            "FROM requirement.delivery_gate AS gate "
            "WHERE gate.requirement_id=:requirement_id "
            "UNION ALL "
            "SELECT 'DELIVERY_GATE_ASSIGNMENT', assignment.assigned_at, assignment.id, "
            "to_jsonb(assignment) FROM requirement.delivery_gate_assignment AS assignment "
            "JOIN requirement.delivery_gate AS assignment_gate "
            "ON assignment_gate.id=assignment.gate_id "
            "WHERE assignment_gate.requirement_id=:requirement_id "
            "UNION ALL "
            "SELECT 'DELIVERY_DECISION', decision.decided_at, decision.id, to_jsonb(decision) "
            "FROM requirement.delivery_decision AS decision "
            "JOIN requirement.delivery_gate AS decision_gate ON decision_gate.id=decision.gate_id "
            "WHERE decision_gate.requirement_id=:requirement_id"
            ") SELECT fact_type, occurred_at, fact_id, fact FROM history "
            + cursor_clause
            + "ORDER BY occurred_at DESC, fact_type DESC, fact_id DESC LIMIT :limit"
        )
        return list(
            self.db.execute(
                statement,
                {
                    "requirement_id": requirement_id,
                    "before_occurred_at": before_occurred_at,
                    "before_fact_type": before_fact_type,
                    "before_fact_id": before_fact_id,
                    "limit": limit,
                },
            ).mappings()
        )

    def decide_delivery_gate(
        self,
        gate_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.delivery_gate SET state='DECIDED', "
                    "decided_at=:now, revision=revision + 1 WHERE id=:gate_id "
                    "AND state='OPEN' AND revision=:expected_revision RETURNING *"
                ),
                {"gate_id": gate_id, "expected_revision": expected_revision, "now": now},
            )
            .mappings()
            .one_or_none()
        )

    def apply_acceptance_decision(
        self,
        requirement_id: str,
        *,
        gate_id: str,
        expected_revision: int,
        state: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET state=:state, "
                    "revision=revision + 1, updated_at=:now WHERE id=:requirement_id "
                    "AND state='AWAITING_ACCEPTANCE' AND revision=:expected_revision "
                    "AND current_acceptance_gate_id=:gate_id RETURNING *"
                ),
                {
                    "requirement_id": requirement_id,
                    "gate_id": gate_id,
                    "expected_revision": expected_revision,
                    "state": state,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def invalidate_current_delivery_evidence(
        self,
        requirement_id: str,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        formal_cycle_busy = self.db.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM requirement.work_item "
                "WHERE requirement_id=:requirement_id AND formal_delivery_state IN ("
                "'MR_PENDING', 'MERGE_PENDING', 'RECONCILIATION_PENDING'))"
            ),
            {"requirement_id": requirement_id},
        ).scalar_one()
        if formal_cycle_busy:
            raise FormalDeliveryConflict("Formal Delivery cycle is busy")
        self.db.execute(
            text(
                "UPDATE requirement.work_item SET state='VERIFYING', "
                "revision=revision + 1, updated_at=:now "
                "WHERE requirement_id=:requirement_id AND state='AWAITING_MERGE' "
                "AND integration_delivery_state='INTEGRATED'"
            ),
            {"requirement_id": requirement_id, "now": now},
        )
        self.db.execute(
            text(
                "UPDATE requirement.delivery_decision AS decision "
                "SET validity='INVALIDATED', invalidated_at=:now, "
                "invalidation_reason=:reason FROM requirement.delivery_gate AS gate "
                "WHERE decision.gate_id=gate.id AND gate.requirement_id=:requirement_id "
                "AND decision.validity='CURRENT'"
            ),
            {"requirement_id": requirement_id, "reason": reason, "now": now},
        )
        self.db.execute(
            text(
                "UPDATE requirement.delivery_gate SET state='INVALIDATED', "
                "invalidated_at=:now, invalidation_reason=:reason, revision=revision + 1 "
                "WHERE requirement_id=:requirement_id AND state IN ('OPEN', 'DECIDED')"
            ),
            {"requirement_id": requirement_id, "reason": reason, "now": now},
        )
        self.db.execute(
            text(
                "UPDATE requirement.integration_baseline_selection "
                "SET invalidated_at=:now, invalidation_reason=:reason "
                "WHERE requirement_id=:requirement_id AND invalidated_at IS NULL"
            ),
            {"requirement_id": requirement_id, "reason": reason, "now": now},
        )
        self.db.execute(
            text(
                "UPDATE requirement.requirement SET "
                "current_integration_baseline_selection_id=NULL, "
                "current_acceptance_gate_id=NULL WHERE id=:requirement_id"
            ),
            {"requirement_id": requirement_id},
        )

    def formal_invalidation_block_reason(self, requirement_id: str) -> str | None:
        return self.db.execute(
            text(
                "SELECT formal_blocked_reason_code FROM requirement.work_item "
                "WHERE requirement_id=:requirement_id "
                "AND formal_delivery_state='BLOCKED' "
                "AND formal_blocked_reason_code IN ("
                "'EXTERNAL_MERGE_DRIFT', 'HEAD_SHA_CHANGED', "
                "'NO_DELIVERY_COMMIT', 'SOURCE_BRANCH_MISSING_AFTER_INTEGRATION') "
                "ORDER BY formal_updated_at, id LIMIT 1"
            ),
            {"requirement_id": requirement_id},
        ).scalar_one_or_none()

    def has_pending_formal_delivery(self, requirement_id: str) -> bool:
        return bool(
            self.db.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM requirement.work_item "
                    "WHERE requirement_id=:requirement_id "
                    "AND formal_delivery_state IN ("
                    "'MR_PENDING', 'MERGE_PENDING', 'RECONCILIATION_PENDING'))"
                ),
                {"requirement_id": requirement_id},
            ).scalar_one()
        )

    def advance_evidence_input(
        self,
        requirement_id: str,
        *,
        expected_revision: int,
        state: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET state=:state, "
                    "requirement_version=requirement_version + 1, revision=revision + 1, "
                    "updated_at=:now WHERE id=:requirement_id AND revision=:expected_revision "
                    "RETURNING *"
                ),
                {
                    "requirement_id": requirement_id,
                    "expected_revision": expected_revision,
                    "state": state,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def touch_requirement(
        self,
        requirement_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET revision=revision + 1, updated_at=:now "
                    "WHERE id=:requirement_id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "requirement_id": requirement_id,
                    "expected_revision": expected_revision,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def list_requirements(
        self,
        *,
        workspace_id: str,
        after_created_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[Any]:
        if after_created_at is None:
            statement = text(
                "SELECT * FROM requirement.requirement WHERE workspace_id=:workspace_id "
                "ORDER BY created_at, id LIMIT :limit"
            )
            parameters: dict[str, object] = {
                "workspace_id": workspace_id,
                "limit": limit,
            }
        else:
            statement = text(
                "SELECT * FROM requirement.requirement WHERE workspace_id=:workspace_id "
                "AND (created_at, id) > (:after_created_at, CAST(:after_id AS UUID)) "
                "ORDER BY created_at, id LIMIT :limit"
            )
            parameters = {
                "workspace_id": workspace_id,
                "after_created_at": after_created_at,
                "after_id": after_id,
                "limit": limit,
            }
        return list(self.db.execute(statement, parameters).mappings())

    def insert_work_item(self, **values: Any) -> Any:
        parameters = {
            **values,
            "required_capabilities": json.dumps(
                values["required_capabilities"],
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.work_item "
                    "(id, requirement_id, created_by, human_owner_id, executor_type, "
                    "executor_id, required_capabilities, assignment_state, repository_state, "
                    "state, repository_id, formal_delivery_state, revision, created_at, "
                    "updated_at) VALUES "
                    "(:id, :requirement_id, :created_by, :human_owner_id, :executor_type, "
                    ":executor_id, CAST(:required_capabilities AS JSONB), :assignment_state, "
                    ":repository_state, :state, :repository_id, 'NOT_STARTED', :revision, "
                    ":now, :now) "
                    "RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def work_items(self, requirement_id: str) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM requirement.work_item "
                    "WHERE requirement_id=:requirement_id ORDER BY created_at, id"
                ),
                {"requirement_id": requirement_id},
            ).mappings()
        )

    def requirement_delivery_snapshot(self, requirement_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT requirement.id, requirement.requirement_version, "
                    "requirement.required_work_item_set_version, "
                    "requirement.required_work_item_set_hash, "
                    "COALESCE(array_agg(work_item.id::text ORDER BY work_item.id) "
                    "FILTER (WHERE work_item.id IS NOT NULL), ARRAY[]::text[]) "
                    "AS work_item_ids "
                    "FROM requirement.requirement AS requirement "
                    "LEFT JOIN requirement.work_item AS work_item "
                    "ON work_item.requirement_id=requirement.id "
                    "WHERE requirement.id=:requirement_id "
                    "GROUP BY requirement.id, requirement.requirement_version, "
                    "requirement.required_work_item_set_version, "
                    "requirement.required_work_item_set_hash"
                ),
                {"requirement_id": requirement_id},
            )
            .mappings()
            .one_or_none()
        )

    def work_item_by_id(
        self,
        work_item_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.work_item WHERE id=:id{suffix}"),
                {"id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_sdd_artifact_version(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.sdd_artifact_version "
                    "(artifact_id, version, requirement_id, sha256, state, media_type, "
                    "trust, content, created_by, created_at) VALUES "
                    "(:artifact_id, :version, :requirement_id, :sha256, :state, "
                    ":media_type, :trust, :content, :created_by, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def sdd_artifact_version(
        self,
        requirement_id: str,
        artifact_id: str,
        version: int,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.sdd_artifact_version "
                    "WHERE requirement_id=:requirement_id "
                    "AND artifact_id=:artifact_id AND version=:version"
                ),
                {
                    "requirement_id": requirement_id,
                    "artifact_id": artifact_id,
                    "version": version,
                },
            )
            .mappings()
            .one_or_none()
        )

    def sdd_artifact_version_by_identity(self, artifact_id: str, version: int) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.sdd_artifact_version "
                    "WHERE artifact_id=:artifact_id AND version=:version"
                ),
                {"artifact_id": artifact_id, "version": version},
            )
            .mappings()
            .one_or_none()
        )

    def latest_sdd_artifact_version(
        self,
        requirement_id: str,
        artifact_id: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.sdd_artifact_version "
                    "WHERE requirement_id=:requirement_id AND artifact_id=:artifact_id "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"requirement_id": requirement_id, "artifact_id": artifact_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_work_item_assignment(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.work_item_assignment "
                    "(id, work_item_id, assignee_id, assigned_by, reason, revision, "
                    "assigned_at) VALUES "
                    "(:id, :work_item_id, :assignee_id, :assigned_by, :reason, :revision, "
                    ":now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def current_work_item_assignment(
        self,
        work_item_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.work_item_assignment "
                    "WHERE work_item_id=:work_item_id AND superseded_at IS NULL"
                    f"{suffix}"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def supersede_work_item_assignment(
        self,
        assignment_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item_assignment SET superseded_at=:now "
                    "WHERE id=:id AND revision=:expected_revision "
                    "AND superseded_at IS NULL RETURNING *"
                ),
                {
                    "id": assignment_id,
                    "expected_revision": expected_revision,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def update_requirement_plan(
        self,
        requirement_id: str,
        *,
        expected_revision: int,
        required_work_item_set_hash: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET "
                    "requirement_version=requirement_version + 1, "
                    "required_work_item_set_version=required_work_item_set_version + 1, "
                    "required_work_item_set_hash=:required_work_item_set_hash, "
                    "current_sdd_baseline_id=NULL, revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision AND state='PREPARING' "
                    "RETURNING *"
                ),
                {
                    "id": requirement_id,
                    "expected_revision": expected_revision,
                    "required_work_item_set_hash": required_work_item_set_hash,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def assign_work_item_projection(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        human_owner_id: str,
        repository_state: str,
        repository_blocked_reason_code: str | None,
        repository_blocked_at: datetime | None,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET human_owner_id=:human_owner_id, "
                    "executor_id=:human_owner_id, assignment_state='ASSIGNED', "
                    "repository_state=:repository_state, "
                    "repository_blocked_reason_code=:repository_blocked_reason_code, "
                    "repository_blocked_at=:repository_blocked_at, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision "
                    "AND integration_delivery_state='NOT_STARTED' RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "human_owner_id": human_owner_id,
                    "repository_state": repository_state,
                    "repository_blocked_reason_code": repository_blocked_reason_code,
                    "repository_blocked_at": repository_blocked_at,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def bind_work_item(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        base_commit_sha: str,
        task_branch: str,
        state: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET repository_state='BOUND', "
                    "base_commit_sha=:base_commit_sha, task_branch=:task_branch, state=:state, "
                    "repository_blocked_reason_code=NULL, repository_blocked_at=NULL, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision "
                    "AND repository_state IN ('WAITING_REPOSITORY', 'BLOCKED') RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "base_commit_sha": base_commit_sha,
                    "task_branch": task_branch,
                    "state": state,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def block_work_item(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET repository_state='BLOCKED', "
                    "base_commit_sha=NULL, task_branch=NULL, state='DRAFT', "
                    "repository_blocked_reason_code=:reason_code, "
                    "repository_blocked_at=:now, revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision "
                    "AND repository_state IN ('WAITING_REPOSITORY', 'BLOCKED') RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "reason_code": reason_code,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def update_work_item_delivery(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        state: str,
        delivery_state: str,
        binding_id: str | None,
        blocked_reason: str | None,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET state=:state, "
                    "integration_delivery_state=:delivery_state, "
                    "integration_merge_request_binding_id=CAST(:binding_id AS UUID), "
                    "integration_blocked_reason_code=:blocked_reason, "
                    "integration_updated_at=:now, revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "state": state,
                    "delivery_state": delivery_state,
                    "binding_id": binding_id,
                    "blocked_reason": blocked_reason,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def update_work_item_formal_delivery(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        state: str,
        formal_state: str,
        binding_id: str | None,
        blocked_reason: str | None,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET state=:state, "
                    "formal_delivery_state=:formal_state, "
                    "formal_merge_request_binding_id=CAST(:binding_id AS UUID), "
                    "formal_blocked_reason_code=:blocked_reason, formal_updated_at=:now, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "state": state,
                    "formal_state": formal_state,
                    "binding_id": binding_id,
                    "blocked_reason": blocked_reason,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def reopen_work_item_for_rework(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        formal_state: str,
        formal_binding_id: str | None,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET state='IN_PROGRESS', "
                    "integration_delivery_state='IMPLEMENTING', "
                    "integration_merge_request_binding_id=NULL, "
                    "integration_blocked_reason_code=NULL, integration_updated_at=:now, "
                    "formal_delivery_state=:formal_state, "
                    "formal_merge_request_binding_id=CAST(:formal_binding_id AS UUID), "
                    "formal_blocked_reason_code=NULL, formal_updated_at=:now, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "formal_state": formal_state,
                    "formal_binding_id": formal_binding_id,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def reopen_integrated_work_items_for_rework(
        self,
        requirement_id: str,
        *,
        now: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET state='IN_PROGRESS', "
                    "integration_delivery_state='IMPLEMENTING', "
                    "integration_merge_request_binding_id=NULL, "
                    "integration_blocked_reason_code=NULL, integration_updated_at=:now, "
                    "formal_delivery_state=CASE "
                    "WHEN formal_merge_request_binding_id IS NULL THEN 'NOT_STARTED' "
                    "ELSE 'MR_OPEN' END, "
                    "formal_blocked_reason_code=NULL, formal_updated_at=:now, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE requirement_id=:requirement_id "
                    "AND integration_delivery_state='INTEGRATED' "
                    "AND formal_delivery_state<>'MERGED' RETURNING *"
                ),
                {"requirement_id": requirement_id, "now": now},
            ).mappings()
        )

    def reopen_formal_invalidation_blocks_for_rework(
        self,
        requirement_id: str,
        *,
        now: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET state='IN_PROGRESS', "
                    "integration_delivery_state='IMPLEMENTING', "
                    "integration_merge_request_binding_id=NULL, "
                    "integration_blocked_reason_code=NULL, integration_updated_at=:now, "
                    "formal_delivery_state=CASE "
                    "WHEN formal_merge_request_binding_id IS NULL THEN 'NOT_STARTED' "
                    "ELSE 'MR_OPEN' END, "
                    "formal_blocked_reason_code=NULL, formal_updated_at=:now, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE requirement_id=:requirement_id "
                    "AND formal_delivery_state='BLOCKED' "
                    "AND formal_blocked_reason_code IN ("
                    "'EXTERNAL_MERGE_DRIFT', 'HEAD_SHA_CHANGED', "
                    "'NO_DELIVERY_COMMIT', 'SOURCE_BRANCH_MISSING_AFTER_INTEGRATION') "
                    "RETURNING *"
                ),
                {"requirement_id": requirement_id, "now": now},
            ).mappings()
        )

    def formal_delivery_context(self, work_item_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT requirement.id AS requirement_id, "
                    "requirement.revision AS requirement_revision, "
                    "requirement.requirement_version, requirement.workspace_id, "
                    "requirement.state AS requirement_state, "
                    "requirement.current_integration_baseline_selection_id, "
                    "requirement.current_acceptance_gate_id, "
                    "work_item.id AS work_item_id, work_item.revision AS work_item_revision, "
                    "work_item.state AS work_item_state, work_item.repository_id, "
                    "work_item.task_branch, work_item.human_owner_id, "
                    "work_item.integration_delivery_state, "
                    "work_item.formal_delivery_state, "
                    "work_item.formal_merge_request_binding_id, "
                    "acceptance.id AS acceptance_decision_id, "
                    "acceptance.outcome AS acceptance_outcome, "
                    "acceptance.validity AS acceptance_validity, "
                    "acceptance.integration_baseline_id, "
                    "acceptance.integration_baseline_hash, "
                    "formal_gate.id AS formal_gate_id, "
                    "formal_gate.selection_id AS formal_gate_selection_id, "
                    "formal_gate.subject_head_sha AS formal_gate_head_sha, "
                    "formal_gate.state AS formal_gate_state, "
                    "formal_decision.id AS formal_review_decision_id, "
                    "formal_decision.outcome AS formal_review_outcome, "
                    "formal_decision.validity AS formal_review_validity "
                    "FROM requirement.work_item AS work_item "
                    "JOIN requirement.requirement AS requirement "
                    "ON requirement.id=work_item.requirement_id "
                    "LEFT JOIN requirement.delivery_decision AS acceptance "
                    "ON acceptance.gate_id=requirement.current_acceptance_gate_id "
                    "LEFT JOIN LATERAL ("
                    "SELECT * FROM requirement.delivery_gate "
                    "WHERE requirement_id=requirement.id AND work_item_id=work_item.id "
                    "AND gate_type='FORMAL_MR_REVIEW' "
                    "ORDER BY "
                    "(selection_id=requirement.current_integration_baseline_selection_id) "
                    "DESC, created_at DESC, id DESC LIMIT 1"
                    ") AS formal_gate ON TRUE "
                    "LEFT JOIN requirement.delivery_decision AS formal_decision "
                    "ON formal_decision.gate_id=formal_gate.id "
                    "WHERE work_item.id=:work_item_id"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def delivery_gate_by_formal_binding(self, binding_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.delivery_gate "
                    "WHERE gate_type='FORMAL_MR_REVIEW' "
                    "AND formal_merge_request_binding_id=:binding_id "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"binding_id": binding_id},
            )
            .mappings()
            .one_or_none()
        )

    def required_formal_delivery_states(self, requirement_id: str) -> tuple[str, ...]:
        return tuple(
            self.db.execute(
                text(
                    "SELECT formal_delivery_state FROM requirement.work_item "
                    "WHERE requirement_id=:requirement_id ORDER BY created_at, id"
                ),
                {"requirement_id": requirement_id},
            ).scalars()
        )

    def required_work_item_states(self, requirement_id: str) -> tuple[str, ...]:
        return tuple(
            self.db.execute(
                text(
                    "SELECT state FROM requirement.work_item "
                    "WHERE requirement_id=:requirement_id ORDER BY created_at, id"
                ),
                {"requirement_id": requirement_id},
            ).scalars()
        )

    def reconcile_planned_work_item_states(
        self,
        requirement_id: str,
        *,
        requirement_state: str,
        now: datetime,
    ) -> list[Any]:
        target = (
            "CASE "
            "WHEN :requirement_state='READY' "
            "AND assignment_state='ASSIGNED' AND repository_state='BOUND' THEN 'READY' "
            "WHEN :requirement_state='CANCELED' THEN 'CANCELED' "
            "ELSE 'DRAFT' END"
        )
        return list(
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET "
                    f"state={target}, revision=revision + 1, updated_at=:now "
                    "WHERE requirement_id=:requirement_id "
                    "AND integration_delivery_state='NOT_STARTED' "
                    f"AND state IS DISTINCT FROM ({target}) RETURNING *"
                ),
                {
                    "requirement_id": requirement_id,
                    "requirement_state": requirement_state,
                    "now": now,
                },
            ).mappings()
        )

    def insert_outbox(self, **values: Any) -> Any:
        parameters = {
            **values,
            "payload": json.dumps(values["payload"], separators=(",", ":")),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.outbox_message "
                    "(id, topic, aggregate_type, aggregate_id, aggregate_version, payload, "
                    "state, attempts, available_at, created_at) VALUES "
                    "(:id, :topic, :aggregate_type, :aggregate_id, :aggregate_version, "
                    "CAST(:payload AS JSONB), 'PENDING', 0, :now, :now) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def claim_binding_requests(
        self,
        *,
        limit: int,
        available_before: datetime,
        lease_until: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "WITH candidates AS ("
                    "SELECT id FROM requirement.outbox_message "
                    "WHERE topic='requirement.repository-binding.requested' "
                    "AND state IN ('PENDING', 'FAILED') "
                    "AND available_at <= :available_before "
                    "ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE requirement.outbox_message AS message "
                    "SET attempts=message.attempts + 1, available_at=:lease_until "
                    "FROM candidates WHERE message.id=candidates.id RETURNING message.*"
                ),
                {
                    "available_before": available_before,
                    "lease_until": lease_until,
                    "limit": limit,
                },
            ).mappings()
        )

    def claim_delivery_requests(
        self,
        *,
        limit: int,
        available_before: datetime,
        lease_until: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "WITH candidates AS ("
                    "SELECT id FROM requirement.outbox_message "
                    "WHERE topic IN ("
                    "'requirement.integration-merge-request.requested', "
                    "'requirement.integration-merge.requested') "
                    "AND state IN ('PENDING', 'FAILED') "
                    "AND available_at <= :available_before "
                    "ORDER BY available_at, created_at, "
                    "CASE topic "
                    "WHEN 'requirement.integration-merge-request.requested' THEN 0 "
                    "ELSE 1 END, id FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE requirement.outbox_message AS message "
                    "SET attempts=message.attempts + 1, available_at=:lease_until "
                    "FROM candidates WHERE message.id=candidates.id RETURNING message.*"
                ),
                {
                    "available_before": available_before,
                    "lease_until": lease_until,
                    "limit": limit,
                },
            ).mappings()
        )

    def claim_evidence_requests(
        self,
        *,
        limit: int,
        available_before: datetime,
        lease_until: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "WITH candidates AS ("
                    "SELECT id FROM requirement.outbox_message "
                    "WHERE topic IN ('requirement.external-validation.submitted', "
                    "'requirement.integration-baseline.requested') "
                    "AND state IN ('PENDING', 'FAILED') "
                    "AND available_at <= :available_before "
                    "ORDER BY available_at, created_at, "
                    "CASE topic WHEN 'requirement.external-validation.submitted' "
                    "THEN 0 ELSE 1 END, id FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE requirement.outbox_message AS message "
                    "SET attempts=message.attempts + 1, available_at=:lease_until "
                    "FROM candidates WHERE message.id=candidates.id RETURNING message.*"
                ),
                {
                    "available_before": available_before,
                    "lease_until": lease_until,
                    "limit": limit,
                },
            ).mappings()
        )

    def claim_formal_delivery_requests(
        self,
        *,
        limit: int,
        available_before: datetime,
        lease_until: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "WITH candidates AS ("
                    "SELECT id FROM requirement.outbox_message "
                    "WHERE topic IN ('requirement.formal-merge-request.requested', "
                    "'requirement.formal-merge.requested') "
                    "AND state IN ('PENDING', 'FAILED') "
                    "AND available_at <= :available_before "
                    "ORDER BY available_at, created_at, "
                    "CASE topic WHEN 'requirement.formal-merge-request.requested' "
                    "THEN 0 ELSE 1 END, id FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE requirement.outbox_message AS message "
                    "SET attempts=message.attempts + 1, available_at=:lease_until "
                    "FROM candidates WHERE message.id=candidates.id RETURNING message.*"
                ),
                {
                    "available_before": available_before,
                    "lease_until": lease_until,
                    "limit": limit,
                },
            ).mappings()
        )

    def outbox_by_id(self, message_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.outbox_message WHERE id=:id{suffix}"),
                {"id": message_id},
            )
            .mappings()
            .one_or_none()
        )

    def repository_binding_context(self, work_item_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT requirement.id AS requirement_id, "
                    "requirement.type AS requirement_type, "
                    "requirement.title AS requirement_title, "
                    "requirement.workspace_id, work_item.id AS work_item_id, "
                    "work_item.revision AS work_item_revision, work_item.repository_id, "
                    "work_item.assignment_state, work_item.human_owner_id, "
                    "work_item.required_capabilities "
                    "FROM requirement.work_item AS work_item "
                    "JOIN requirement.requirement AS requirement "
                    "ON requirement.id=work_item.requirement_id "
                    "WHERE work_item.id=:work_item_id"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def integration_delivery_context(self, work_item_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT requirement.id AS requirement_id, "
                    "requirement.revision AS requirement_revision, "
                    "requirement.state AS requirement_state, requirement.workspace_id, "
                    "work_item.id AS work_item_id, work_item.revision AS work_item_revision, "
                    "work_item.state AS work_item_state, work_item.repository_id, "
                    "work_item.repository_state, work_item.human_owner_id, "
                    "work_item.required_capabilities, work_item.base_commit_sha, "
                    "work_item.task_branch, work_item.integration_delivery_state, "
                    "work_item.integration_merge_request_binding_id, "
                    "delivery_request.payload->>'actorId' AS request_actor_id "
                    "FROM requirement.work_item AS work_item "
                    "JOIN requirement.requirement AS requirement "
                    "ON requirement.id=work_item.requirement_id "
                    "LEFT JOIN LATERAL ("
                    "SELECT payload FROM requirement.outbox_message "
                    "WHERE topic IN ("
                    "'requirement.integration-merge-request.requested', "
                    "'requirement.integration-merge.requested') "
                    "AND payload->>'workItemId'=work_item.id::text "
                    "AND payload->>'workItemRevision' ~ '^[0-9]+$' "
                    "AND (payload->>'workItemRevision')::bigint <= work_item.revision "
                    "AND (work_item.integration_delivery_state NOT IN "
                    "('MR_PENDING', 'MERGE_PENDING') "
                    "OR (work_item.integration_delivery_state='MR_PENDING' AND topic="
                    "'requirement.integration-merge-request.requested') "
                    "OR (work_item.integration_delivery_state='MERGE_PENDING' AND topic="
                    "'requirement.integration-merge.requested')) "
                    "ORDER BY (payload->>'workItemRevision')::bigint DESC, "
                    "created_at DESC, id DESC LIMIT 1"
                    ") AS delivery_request ON TRUE "
                    "WHERE work_item.id=:work_item_id"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def publish_outbox(self, message_id: str, *, now: datetime) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.outbox_message SET state='PUBLISHED', "
                    "published_at=:now, last_error_code=NULL "
                    "WHERE id=:id AND state IN ('PENDING', 'FAILED') RETURNING *"
                ),
                {"id": message_id, "now": now},
            )
            .mappings()
            .one_or_none()
        )

    def release_outbox(
        self,
        message_id: str,
        *,
        error_code: str,
        available_at: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.outbox_message SET state='FAILED', "
                    "last_error_code=:error_code, available_at=:available_at, "
                    "published_at=NULL WHERE id=:id "
                    "AND state IN ('PENDING', 'FAILED') RETURNING *"
                ),
                {
                    "id": message_id,
                    "error_code": error_code,
                    "available_at": available_at,
                },
            )
            .mappings()
            .one_or_none()
        )

    def outbox_by_aggregate(
        self,
        aggregate_id: str,
        *,
        aggregate_version: int,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM requirement.outbox_message "
                    "WHERE aggregate_id=:aggregate_id "
                    "AND aggregate_version=:aggregate_version ORDER BY created_at, id"
                ),
                {
                    "aggregate_id": aggregate_id,
                    "aggregate_version": aggregate_version,
                },
            ).mappings()
        )

    def update_requirement_state(
        self,
        requirement_id: str,
        *,
        expected_revision: int,
        state: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET state=:state, revision=revision + 1, "
                    "updated_at=:now WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": requirement_id,
                    "expected_revision": expected_revision,
                    "state": state,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_sdd_baseline(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.sdd_baseline "
                    "(id, requirement_id, requirement_version, artifact_id, "
                    "artifact_version, artifact_hash, route_snapshot_version, "
                    "route_snapshot_hash, created_by, created_at) VALUES "
                    "(:id, :requirement_id, :requirement_version, :artifact_id, "
                    ":artifact_version, :artifact_hash, :route_snapshot_version, "
                    ":route_snapshot_hash, :created_by, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def set_current_sdd_baseline(
        self,
        requirement_id: str,
        *,
        baseline_id: str,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET current_sdd_baseline_id=:baseline_id, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": requirement_id,
                    "baseline_id": baseline_id,
                    "expected_revision": expected_revision,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def sdd_baseline_by_id(self, baseline_id: str) -> Any:
        return (
            self.db.execute(
                text("SELECT * FROM requirement.sdd_baseline WHERE id=:id"),
                {"id": baseline_id},
            )
            .mappings()
            .one_or_none()
        )

    def sdd_baseline_by_artifact(
        self,
        requirement_id: str,
        artifact_id: str,
        artifact_version: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.sdd_baseline "
                    "WHERE requirement_id=:requirement_id AND artifact_id=:artifact_id "
                    "AND artifact_version=:artifact_version ORDER BY created_at, id LIMIT 1"
                ),
                {
                    "requirement_id": requirement_id,
                    "artifact_id": artifact_id,
                    "artifact_version": artifact_version,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_gate(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.gate_instance "
                    "(id, gate_type, requirement_id, requirement_version, sdd_baseline_id, "
                    "artifact_id, artifact_version, artifact_hash, route_snapshot_version, "
                    "route_snapshot_hash, policy_code, policy_version, "
                    "policy_snapshot_hash, state, revision, created_at) VALUES "
                    "(:id, :gate_type, :requirement_id, :requirement_version, "
                    ":sdd_baseline_id, :artifact_id, :artifact_version, :artifact_hash, "
                    ":route_snapshot_version, :route_snapshot_hash, :policy_code, "
                    ":policy_version, :policy_snapshot_hash, :state, :revision, :now) "
                    "RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def gate_by_id(self, gate_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.gate_instance WHERE id=:id{suffix}"),
                {"id": gate_id},
            )
            .mappings()
            .one_or_none()
        )

    def gate_by_baseline_id(self, baseline_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.gate_instance "
                    "WHERE sdd_baseline_id=:baseline_id ORDER BY created_at, id LIMIT 1"
                ),
                {"baseline_id": baseline_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_gate_assignment(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.gate_assignment "
                    "(id, gate_instance_id, default_reviewer_id, current_reviewer_id, "
                    "revision, assigned_at) VALUES "
                    "(:id, :gate_instance_id, :default_reviewer_id, "
                    ":current_reviewer_id, :revision, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def current_gate_assignment(self, gate_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.gate_assignment "
                    "WHERE gate_instance_id=:gate_id AND superseded_at IS NULL"
                    f"{suffix}"
                ),
                {"gate_id": gate_id},
            )
            .mappings()
            .one_or_none()
        )

    def current_work_item_assignments(self, requirement_id: str) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT assignment.* FROM requirement.work_item_assignment AS assignment "
                    "JOIN requirement.work_item AS item ON item.id=assignment.work_item_id "
                    "WHERE item.requirement_id=:requirement_id "
                    "AND assignment.superseded_at IS NULL "
                    "ORDER BY item.created_at, item.id"
                ),
                {"requirement_id": requirement_id},
            ).mappings()
        )

    def supersede_gate_assignment(
        self,
        assignment_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.gate_assignment SET superseded_at=:now "
                    "WHERE id=:id AND revision=:expected_revision "
                    "AND superseded_at IS NULL RETURNING *"
                ),
                {
                    "id": assignment_id,
                    "expected_revision": expected_revision,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def reassign_gate(
        self,
        gate_id: str,
        *,
        expected_revision: int,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.gate_instance SET revision=revision + 1 "
                    "WHERE id=:id AND revision=:expected_revision AND state='OPEN' "
                    "RETURNING *"
                ),
                {"id": gate_id, "expected_revision": expected_revision},
            )
            .mappings()
            .one_or_none()
        )

    def insert_decision(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.decision "
                    "(id, gate_instance_id, gate_assignment_id, reviewer_id, outcome, "
                    "reason, subject_revision, decided_at) VALUES "
                    "(:id, :gate_instance_id, :gate_assignment_id, :reviewer_id, :outcome, "
                    ":reason, :subject_revision, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def decision_by_gate_id(self, gate_id: str) -> Any:
        return (
            self.db.execute(
                text("SELECT * FROM requirement.decision WHERE gate_instance_id=:gate_id"),
                {"gate_id": gate_id},
            )
            .mappings()
            .one_or_none()
        )

    def close_gate(
        self,
        gate_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.gate_instance SET state='DECIDED', "
                    "revision=revision + 1, decided_at=:now "
                    "WHERE id=:id AND revision=:expected_revision AND state='OPEN' "
                    "RETURNING *"
                ),
                {"id": gate_id, "expected_revision": expected_revision, "now": now},
            )
            .mappings()
            .one_or_none()
        )
