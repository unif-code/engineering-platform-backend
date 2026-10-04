from typing import Protocol


class RebaseAuthorizationPort(Protocol):
    def check(self, *, raw_session: str, actor_id: str) -> None: ...
