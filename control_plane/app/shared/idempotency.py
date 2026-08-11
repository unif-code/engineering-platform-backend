import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from control_plane.app.shared.security import seal, unseal

_REPLAY_METADATA = {"kind": "http-response", "schemaVersion": 1}
# Preserve Task 6 fingerprints so in-flight identity keys remain replayable after
# extracting the engine into this module-neutral helper.
_FINGERPRINT_KDF_INFO = b"engineering-platform/identity/idempotency-fingerprint/v1"


class IdempotencyRepository(Protocol):
    def claim_idempotency(self, **values: Any) -> bool: ...

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool: ...


class IdempotencyConflict(RuntimeError):
    """The command key is already bound to another request or unfinished result."""


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
    idempotency_sealing_key: bytes,
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
    fingerprint_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_FINGERPRINT_KDF_INFO,
    ).derive(idempotency_sealing_key)
    return hmac.new(fingerprint_key, canonical, hashlib.sha256).hexdigest()


def _replay(row: Any, idempotency_sealing_key: bytes) -> IdempotentResponse:
    if (
        row["state"] != "COMPLETED"
        or row["result_metadata"] != _REPLAY_METADATA
        or row["sealed_response"] is None
    ):
        raise IdempotencyConflict("idempotent command is still in progress")
    try:
        plaintext = unseal(row["sealed_response"], idempotency_sealing_key)
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
    repository: IdempotencyRepository,
    *,
    actor: str,
    operation: str,
    key: str,
    fingerprint: str,
    command: Callable[[], IdempotentResponse],
    now: Callable[[], datetime],
    new_id: Callable[[], object],
    idempotency_sealing_key: bytes,
) -> IdempotentExecution:
    """Claim, execute, seal, and complete one command in its caller transaction."""
    claimed = repository.claim_idempotency(
        id=str(new_id()),
        actor=actor,
        operation=operation,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        now=now(),
    )
    row = repository.idempotency_by_scope(actor, operation, key, for_update=True)
    if row is None:
        raise IdempotencyReplayUnavailable("idempotency claim is unavailable")
    if row["request_fingerprint"] != fingerprint:
        raise IdempotencyConflict("Idempotency-Key is bound to a different request")
    if not claimed:
        return IdempotentExecution(response=_replay(row, idempotency_sealing_key), replayed=True)

    response = command()
    envelope = SealedIdempotentEnvelope(
        actor=actor,
        operation=operation,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        response=response,
    )
    sealed_response = seal(
        envelope.model_dump_json(by_alias=True).encode("utf-8"),
        idempotency_sealing_key,
    )
    if not repository.complete_idempotency(
        str(row["id"]),
        http_status=response.status_code,
        result_metadata=_REPLAY_METADATA,
        sealed_response=sealed_response,
        now=now(),
    ):
        raise IdempotencyReplayUnavailable("idempotency completion is unavailable")
    return IdempotentExecution(response=response, replayed=False)
