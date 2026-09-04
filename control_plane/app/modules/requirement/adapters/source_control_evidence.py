from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

import control_plane.app.modules.source_control as source_control
from control_plane.app.modules.requirement.domain import ArtifactEvidenceReference
from control_plane.app.modules.requirement.ports import (
    IntegrationBaselineEvidenceSnapshot,
    IntegrationBaselineEvidenceWorkItem,
)


@dataclass(frozen=True, slots=True)
class SourceControlFacadeEvidenceAdapter:
    engine: Engine
    dependencies: Any

    def get(self, evidence_id: str) -> IntegrationBaselineEvidenceSnapshot:
        with self.engine.connect() as db:
            evidence = source_control.get_integration_baseline_evidence(
                db,
                evidence_id=evidence_id,
                dependencies=self.dependencies,
            )
        return self._snapshot(evidence)

    def get_by_snapshot(
        self, *, delivery_snapshot_id: str, delivery_snapshot_hash: str
    ) -> IntegrationBaselineEvidenceSnapshot:
        with self.engine.connect() as db:
            evidence = source_control.get_integration_baseline_evidence_by_snapshot(
                db,
                delivery_snapshot_id=delivery_snapshot_id,
                delivery_snapshot_hash=delivery_snapshot_hash,
                dependencies=self.dependencies,
            )
        return self._snapshot(evidence)

    @staticmethod
    def _snapshot(
        evidence: source_control.IntegrationBaselineEvidence,
    ) -> IntegrationBaselineEvidenceSnapshot:
        return IntegrationBaselineEvidenceSnapshot(
            id=evidence.id,
            evidence_hash=evidence.evidence_hash,
            delivery_snapshot_id=evidence.delivery_snapshot_id,
            delivery_snapshot_hash=evidence.delivery_snapshot_hash,
            requirement_id=evidence.requirement_id,
            requirement_version=evidence.requirement_version,
            required_work_item_set_version=evidence.required_work_item_set_version,
            required_work_item_set_hash=evidence.required_work_item_set_hash,
            currentness_state=evidence.currentness.state.value,
            currentness_reasons=tuple(
                reason for item in evidence.currentness.items for reason in item.reasons
            ),
            work_items=tuple(
                IntegrationBaselineEvidenceWorkItem(
                    work_item_id=item.work_item_id,
                    repository_id=item.repository_id,
                    task_commit_sha=item.task_commit_sha,
                    integration_merge_commit_sha=item.integration_merge_commit_sha,
                    artifact_references=tuple(
                        ArtifactEvidenceReference(
                            artifact_id=artifact.artifact_id,
                            artifact_version=artifact.artifact_version,
                            artifact_hash=artifact.artifact_hash,
                        )
                        for artifact in item.artifact_references
                    ),
                )
                for item in evidence.items
            ),
            generated_at=evidence.generated_at,
        )
