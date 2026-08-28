from control_plane.app.modules.source_control import AuthorizedRepositorySummaryDto
from control_plane.app.shared.api.camel import CamelModel


class AuthorizedRepositoryResponseDto(CamelModel):
    repository_id: str
    provider: str
    project_path: str
    default_branch: str

    @classmethod
    def from_domain(
        cls,
        value: AuthorizedRepositorySummaryDto,
    ) -> "AuthorizedRepositoryResponseDto":
        return cls.model_validate(value.model_dump())


class AuthorizedRepositoryListResponseDto(CamelModel):
    items: list[AuthorizedRepositoryResponseDto]
