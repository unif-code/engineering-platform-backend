from pydantic import ConfigDict

from control_plane.app.modules.model_gateway.domain import (
    ArchiveReason,
    CreateDeployment,
    Deployment,
    PatchDeployment,
)
from control_plane.app.shared.api.camel import CamelModel


class CreateModelDeploymentRequestDto(CreateDeployment, CamelModel):
    """Unverified candidate only.

    connectionRef accepts model-connection:<identifier>, never credentials or a URL.
    """

    model_config = ConfigDict(populate_by_name=False, extra="forbid")


class PatchModelDeploymentRequestDto(PatchDeployment, CamelModel):
    """Omitted fields stay unchanged; at least one editable field is required.

    Only connectionRef, description and the two token declarations accept null to clear.
    """

    model_config = ConfigDict(populate_by_name=False, extra="forbid")


class ArchiveModelDeploymentRequestDto(CamelModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False)
    reason: ArchiveReason


class ModelDeploymentDto(Deployment, CamelModel):
    """Persisted, unverified declaration.

    DRAFT does not imply availability, activation or routing eligibility.
    """


class ModelDeploymentsResponseDto(CamelModel):
    items: list[ModelDeploymentDto]
    next_cursor: str | None
