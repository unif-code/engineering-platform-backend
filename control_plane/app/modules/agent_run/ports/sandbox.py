from typing import Protocol

from control_plane.app.modules.agent_run.domain import (
    CancelExecutionCommand,
    CancellationReceipt,
    CheckpointAndReleaseCommand,
    FinalizationReceipt,
    FinalizeExecutionCommand,
    GetMaterializationStatusQuery,
    HandoffResult,
    HandoffToChildCommand,
    MaterializationStatus,
    PreviewResult,
    ProvisionMaterializationCommand,
    ProvisionResult,
    PublishPreviewCommand,
    ReconcileLeaseCommand,
    ReconciliationReceipt,
    ReleaseReceipt,
)


class SandboxPort(Protocol):
    def provision_materialization(
        self, command: ProvisionMaterializationCommand
    ) -> ProvisionResult: ...

    def get_materialization_status(
        self, query: GetMaterializationStatusQuery
    ) -> MaterializationStatus: ...

    def publish_preview(self, command: PublishPreviewCommand) -> PreviewResult: ...

    def checkpoint_and_release(self, command: CheckpointAndReleaseCommand) -> ReleaseReceipt: ...

    def handoff_to_child(self, command: HandoffToChildCommand) -> HandoffResult: ...

    def finalize_execution(self, command: FinalizeExecutionCommand) -> FinalizationReceipt: ...

    def cancel_execution(self, command: CancelExecutionCommand) -> CancellationReceipt: ...

    def reconcile_lease(self, command: ReconcileLeaseCommand) -> ReconciliationReceipt: ...
