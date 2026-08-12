from control_plane.app.modules.configuration.domain import PublishedVersion
from control_plane.app.modules.configuration.ports import PolicyOwnerPort


def policy_versions(
    owner: PolicyOwnerPort,
    namespace: str,
    *,
    scope: str = "PLATFORM",
    before_version: int | None = None,
    limit: int = 50,
) -> tuple[list[PublishedVersion], int | None]:
    versions = owner.list_versions(
        namespace,
        scope,
        before_version=before_version,
        limit=limit + 1,
    )
    if len(versions) <= limit:
        return versions, None
    return versions[:limit], versions[limit - 1].version
