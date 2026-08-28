"""Source Control adapters."""

from control_plane.app.modules.source_control.adapters.eligibility import (
    CurrentOwnerEligibilityAdapter,
)
from control_plane.app.modules.source_control.adapters.gitlab import HttpxGitLabAdapter
from control_plane.app.modules.source_control.adapters.gitlab_merge_requests import (
    HttpxGitLabMergeRequestAdapter,
)
from control_plane.app.modules.source_control.adapters.integration_sqlalchemy import (
    SqlAlchemySourceControlIntegrationRepository,
)
from control_plane.app.modules.source_control.adapters.policy import SourceControlDevPolicy
from control_plane.app.modules.source_control.adapters.requirement import (
    RequirementFacadeBindingAdapter,
)
from control_plane.app.modules.source_control.adapters.requirement_delivery import (
    RequirementFacadeDeliveryAdapter,
)
from control_plane.app.modules.source_control.adapters.secrets import (
    DevSecretReferenceResolver,
)
from control_plane.app.modules.source_control.adapters.settings import (
    SourceControlDevSettings,
)
from control_plane.app.modules.source_control.adapters.sqlalchemy import (
    SqlAlchemySourceControlRepository,
)

__all__ = [
    "CurrentOwnerEligibilityAdapter",
    "DevSecretReferenceResolver",
    "HttpxGitLabAdapter",
    "HttpxGitLabMergeRequestAdapter",
    "SqlAlchemySourceControlIntegrationRepository",
    "RequirementFacadeBindingAdapter",
    "RequirementFacadeDeliveryAdapter",
    "SourceControlDevPolicy",
    "SourceControlDevSettings",
    "SqlAlchemySourceControlRepository",
]
