from control_plane.app.bootstrap import app
from control_plane.app.bootstrap.source_control_runtime import default_source_control_collaborators
from control_plane.app.modules.requirement.adapters import (
    DeliveryGatePolicyAdapter,
    DeliveryReviewerGuardAdapter,
    SourceControlFacadeEvidenceAdapter,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlEvidenceRepository,
)


def test_default_requirement_and_worker_share_live_owner_policy_and_qualification() -> None:
    dependencies = app.requirement_dependencies()
    assert isinstance(dependencies.delivery_gate_policies, DeliveryGatePolicyAdapter)
    assert isinstance(dependencies.delivery_reviewer_guard, DeliveryReviewerGuardAdapter)
    assert isinstance(dependencies.integration_evidence, SourceControlFacadeEvidenceAdapter)
    collaborators = default_source_control_collaborators()
    assert collaborators.qualification is dependencies.delivery_reviewer_guard.qualification
    assert collaborators.requirement_policy is dependencies.delivery_gate_policies.policy
    assert collaborators.requirement_dependencies is dependencies
    assert dependencies.integration_evidence.dependencies.evidence_repository_factory is (
        SqlAlchemySourceControlEvidenceRepository
    )
