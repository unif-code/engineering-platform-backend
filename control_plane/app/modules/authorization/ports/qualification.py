from typing import Protocol

from control_plane.app.modules.authorization.domain.qualification import (
    ActorAccountFacts,
    ActorQualificationSnapshot,
    ActorWorkspaceFacts,
)


class ActorFactsPort(Protocol):
    def account(self, actor_id: str) -> ActorAccountFacts: ...

    def membership(self, actor_id: str, workspace_id: str) -> ActorWorkspaceFacts: ...


class ActorQualificationPort(Protocol):
    def evaluate(
        self,
        actor_id: str,
        workspace_id: str,
        required_capabilities: tuple[str, ...],
    ) -> ActorQualificationSnapshot: ...
