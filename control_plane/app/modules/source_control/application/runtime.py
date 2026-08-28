from control_plane.app.modules.source_control.domain import (
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.ports import (
    SecretReferencePort,
    SourceControlRepository,
)


def validate_authorized_repository_runtime(
    repository: SourceControlRepository,
    *,
    secrets: SecretReferencePort,
    connection_ref: str,
) -> None:
    for row in repository.authorized_repository_runtime_references():
        if row["connection_ref"] != connection_ref:
            raise SourceControlDependencyUnavailable(
                "Source Control runtime configuration is unavailable"
            )
        secrets.resolve(str(row["credential_secret_ref"]))
        webhook_reference = row["webhook_signing_secret_ref"]
        if webhook_reference is not None:
            secrets.resolve(str(webhook_reference))
