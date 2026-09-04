"""Internal, immutable facts; these are not bearer credentials."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Literal

POLICY_REAUTH_TTL = timedelta(minutes=5)


class PolicyReauthenticationDenied(Exception):
    """Authentication or exact consumed-fact validation failed closed."""


class PolicyReauthenticationUnavailable(Exception):
    """No usable receipt is returned when persistence or commit is uncertain."""


@dataclass(frozen=True, slots=True)
class PolicyReauthBinding:
    actor_id: str
    operation: Literal["POLICY_PUBLISH", "POLICY_ROLLBACK"]
    namespace: str
    scope: str
    draft_id: str
    draft_revision: int
    content_hash: str
    schema_revision: int
    base_version: int
    dependency_versions: tuple[tuple[str, int], ...]
    command_attempt_id: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        if self.operation not in {"POLICY_PUBLISH", "POLICY_ROLLBACK"}:
            raise ValueError("unsupported policy reauthentication operation")
        for value in (
            self.actor_id,
            self.namespace,
            self.scope,
            self.draft_id,
            self.command_attempt_id,
        ):
            if type(value) is not str or not value.strip() or value != value.strip():
                raise ValueError("invalid policy reauthentication binding")
        for revision in (self.draft_revision, self.schema_revision, self.base_version):
            if type(revision) is not int or revision < 1:
                raise ValueError("invalid policy reauthentication revision")
        for value in (self.content_hash, self.request_fingerprint):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("invalid policy reauthentication hash")
        if type(self.dependency_versions) is not tuple:
            raise ValueError("policy dependencies must be immutable")
        names: set[str] = set()
        for item in self.dependency_versions:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or not item[0].strip()
                or item[0] != item[0].strip()
                or item[0] in names
                or type(item[1]) is not int
                or item[1] < 1
            ):
                raise ValueError("invalid policy dependency versions")
            names.add(item[0])
        object.__setattr__(self, "dependency_versions", tuple(sorted(self.dependency_versions)))

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @property
    def canonical_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ConsumedReauthReceipt:
    receipt_id: str
    binding: PolicyReauthBinding
    session_reference: str
    account_version: int
    consumed_at: datetime
    expires_at: datetime

    def matches(
        self,
        binding: PolicyReauthBinding,
        *,
        session_reference: str,
        now: datetime,
    ) -> bool:
        return (
            self.binding == binding
            and self.session_reference == session_reference
            and self.account_version > 0
            and self.consumed_at.tzinfo is not None
            and self.expires_at.tzinfo is not None
            and now.tzinfo is not None
            and self.expires_at == self.consumed_at + POLICY_REAUTH_TTL
            and self.consumed_at <= now < self.expires_at
        )
