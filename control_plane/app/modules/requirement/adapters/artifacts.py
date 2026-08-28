from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import Engine

from control_plane.app.modules.requirement.adapters.sqlalchemy import (
    SqlAlchemyRequirementRepository,
)
from control_plane.app.modules.requirement.domain import ArtifactUnavailable
from control_plane.app.modules.requirement.ports import (
    ArtifactSnapshot,
    ArtifactState,
    ArtifactTrust,
)


@dataclass(frozen=True, slots=True)
class SqlAlchemySddArtifactReader:
    engine: Engine

    def get_snapshot(self, artifact_id: str, artifact_version: str) -> ArtifactSnapshot:
        try:
            version = int(artifact_version)
        except ValueError:
            raise ArtifactUnavailable(artifact_id) from None
        if version < 1 or str(version) != artifact_version:
            raise ArtifactUnavailable(artifact_id)
        with self.engine.connect() as db:
            row = SqlAlchemyRequirementRepository(db).sdd_artifact_version_by_identity(
                artifact_id,
                version,
            )
        if row is None:
            raise ArtifactUnavailable(artifact_id)
        return ArtifactSnapshot(
            id=str(row["artifact_id"]),
            version=str(row["version"]),
            sha256=row["sha256"],
            state=ArtifactState(row["state"]),
            media_type=row["media_type"],
            trust=ArtifactTrust(row["trust"]),
        )


@dataclass(frozen=True, slots=True)
class InMemorySddArtifactReader:
    snapshots: Mapping[tuple[str, str], ArtifactSnapshot]

    def get_snapshot(self, artifact_id: str, artifact_version: str) -> ArtifactSnapshot:
        try:
            return self.snapshots[(artifact_id, artifact_version)]
        except KeyError:
            raise ArtifactUnavailable(artifact_id) from None
