from typing import Protocol


class DraftAuthorizationPort(Protocol):
    def check(self, *, raw_session: str, actor_id: str) -> None: ...
