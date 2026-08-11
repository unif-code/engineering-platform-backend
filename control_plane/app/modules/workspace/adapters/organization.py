from sqlalchemy import Engine

from control_plane.app.modules.organization import (
    OrganizationDependencies,
    direct_reports,
    is_effective_leader,
)
from control_plane.app.modules.workspace.ports import DirectReportView


class SqlAlchemyOrganizationReports:
    def __init__(self, engine: Engine, dependencies: OrganizationDependencies) -> None:
        self.engine = engine
        self.dependencies = dependencies

    def is_effective_leader(self, account_id: str) -> bool:
        with self.engine.connect() as db:
            return is_effective_leader(
                db,
                leader_id=account_id,
                dependencies=self.dependencies,
            )

    def direct_reports(self, leader_id: str) -> list[DirectReportView]:
        with self.engine.connect() as db:
            reports = direct_reports(
                db,
                leader_id=leader_id,
                dependencies=self.dependencies,
            )
        return [DirectReportView(id=report.id) for report in reports]
