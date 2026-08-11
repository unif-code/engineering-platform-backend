import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.ports.repository import IdentityRepository
from control_plane.app.shared.security import seal, unseal

_REPLAY_METADATA = {"kind": "http-response", "schemaVersion": 1}


class IdempotencyConflict(RuntimeError):
    """The command key is already bound to another request or an unfinished result."""


class IdempotencyReplayUnavailable(RuntimeError):
    """A completed command cannot be authenticated and replayed safely."""


class CookieReplay(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["set", "delete"]
    value: str | None = None


class IdempotentResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status_code: int
    body: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)
    cookie: CookieReplay | None = None
    is_problem: bool = False


class IdempotentExecution(BaseModel):
    model_config = ConfigDict(frozen=True)

    response: IdempotentResponse
    replayed: bool


class SealedIdempotentEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor: str
    operation: str
    idempotency_key: str
    request_fingerprint: str
    response: IdempotentResponse


def canonical_request_fingerprint(
    *,
    operation: str,
    method: str,
    path: str,
    body: Mapping[str, object],
) -> str:
    canonical = json.dumps(
        {
            "body": body,
            "method": method.upper(),
            "operation": operation,
            "path": path,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _replay(row: Any, dependencies: IdentityDependencies) -> IdempotentResponse:
    if (
        row["state"] != "COMPLETED"
        or row["result_metadata"] != _REPLAY_METADATA
        or row["sealed_response"] is None
    ):
        raise IdempotencyConflict("idempotent command is still in progress")
    try:
        material = dependencies.secret_manager.load()
        plaintext = unseal(row["sealed_response"], material.idempotency_sealing_key)
        envelope = SealedIdempotentEnvelope.model_validate_json(plaintext)
    except (InvalidTag, ValueError, UnicodeError, ValidationError):
        raise IdempotencyReplayUnavailable("idempotent response is unavailable") from None
    if (
        envelope.actor != row["actor"]
        or envelope.operation != row["operation"]
        or envelope.idempotency_key != row["idempotency_key"]
        or envelope.request_fingerprint != row["request_fingerprint"]
        or envelope.response.status_code != row["http_status"]
    ):
        raise IdempotencyReplayUnavailable("idempotent response is unavailable")
    return envelope.response


def execute_idempotent(
    repository: IdentityRepository,
    *,
    actor: str,
    operation: str,
    key: str,
    fingerprint: str,
    command: Callable[[], IdempotentResponse],
    dependencies: IdentityDependencies,
) -> IdempotentExecution:
    """Claim, execute, seal, and complete one identity command in its caller transaction."""
    now = dependencies.clock.now()
    claimed = repository.claim_idempotency(
        id=str(dependencies.random.uuid4()),
        actor=actor,
        operation=operation,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        now=now,
    )
    row = repository.idempotency_by_scope(
        actor,
        operation,
        key,
        for_update=True,
    )
    if row is None:
        raise IdempotencyReplayUnavailable("idempotency claim is unavailable")
    if row["request_fingerprint"] != fingerprint:
        raise IdempotencyConflict("Idempotency-Key is bound to a different request")
    if not claimed:
        return IdempotentExecution(response=_replay(row, dependencies), replayed=True)

    response = command()
    material = dependencies.secret_manager.load()
    envelope = SealedIdempotentEnvelope(
        actor=actor,
        operation=operation,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        response=response,
    )
    sealed_response = seal(
        envelope.model_dump_json(by_alias=True).encode("utf-8"),
        material.idempotency_sealing_key,
    )
    completed_at = dependencies.clock.now()
    if not repository.complete_idempotency(
        str(row["id"]),
        http_status=response.status_code,
        result_metadata=_REPLAY_METADATA,
        sealed_response=sealed_response,
        now=completed_at,
    ):
        raise IdempotencyReplayUnavailable("idempotency completion is unavailable")
    return IdempotentExecution(response=response, replayed=False)
