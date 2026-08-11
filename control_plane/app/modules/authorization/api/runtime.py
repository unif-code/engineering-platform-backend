from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from control_plane.app.modules.authorization import (
    AuthorizationDependencies,
    DecisionDependencies,
)


@dataclass(frozen=True, slots=True)
class AuthorizationHttpRuntime:
    engine: Engine
    dependencies: AuthorizationDependencies
    decision_dependencies: DecisionDependencies
    organization_summary: Callable[[str], dict[str, Any] | None]
    workspace_summaries: Callable[[str], list[dict[str, Any]]]
