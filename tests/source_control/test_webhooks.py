import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from control_plane.app.modules.source_control import (
    SourceControlDependencies,
    WebhookIdConflict,
    WebhookPayloadInvalid,
    WebhookReplayRejected,
    WebhookSignatureInvalid,
    ingest_signed_gitlab_webhook,
    register_workspace_repository,
    verify_gitlab_standard_webhook,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.api import (
    SourceControlWebhookRuntime,
    create_webhook_router,
)

NOW = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)
REPOSITORY_ID = "gitlab-project-webhook"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000601"
WEBHOOK_ID = "webhook-601"
SIGNING_KEY = b"test-only-webhook-signing-key-32"
SIGNING_TOKEN = "whsec_" + base64.b64encode(SIGNING_KEY).decode("ascii")


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedRandom:
    def __init__(self) -> None:
        self._next = 600

    def uuid4(self) -> UUID:
        self._next += 1
        return UUID(f"90000000-0000-0000-0000-{self._next:012d}")


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append_in_transaction(self, _db: object, envelope: object) -> None:
        self.events.append(envelope)


class FakeSecrets:
    def resolve(self, reference: str) -> str:
        assert reference == "secret-ref:webhook"
        return SIGNING_TOKEN


class WebhookPolicy:
    def next_reconcile_at(self, *, now: datetime, attempts: int) -> datetime:
        return now + timedelta(seconds=max(attempts, 1) * 30)

    def webhook_replay_window(self) -> timedelta:
        return timedelta(minutes=5)


def _body(*, project_id: int = 101, after: str = "a" * 40) -> bytes:
    return json.dumps(
        {
            "object_kind": "push",
            "project": {"id": project_id},
            "ref": "refs/heads/feat/wi-601-source-control",
            "before": "b" * 40,
            "after": after,
            "checkout_sha": after,
        },
        separators=(",", ":"),
    ).encode()


def _sign(body: bytes, *, timestamp: int | None = None) -> str:
    value = timestamp if timestamp is not None else int(NOW.timestamp())
    message = f"{WEBHOOK_ID}.{value}.".encode() + body
    digest = hmac.new(SIGNING_KEY, message, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def _headers(body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    value = timestamp if timestamp is not None else int(NOW.timestamp())
    return {
        "webhook-id": WEBHOOK_ID,
        "webhook-timestamp": str(value),
        "webhook-signature": _sign(body, timestamp=value),
        "x-gitlab-event": "Push Hook",
        "x-gitlab-event-uuid": "provider-event-601",
    }


def _dependencies(engine: Engine) -> SourceControlDependencies:
    return SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=engine,
        requirement=None,
        eligibility=None,
        audit=FakeAudit(),
        clock=FixedClock(),
        random=FixedRandom(),
        policy=WebhookPolicy(),
        webhook_secrets=FakeSecrets(),
    )


def _register(engine: Engine, dependencies: SourceControlDependencies) -> None:
    with engine.begin() as db:
        register_workspace_repository(
            SqlAlchemySourceControlRepository(db),
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref="secret-ref:webhook",
            actor="SYSTEM",
            dependencies=dependencies,
        )


def test_signed_webhook_is_verified_before_deduplicated(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies = _dependencies(isolated_source_control_rw_engine)
    _register(isolated_source_control_rw_engine, dependencies)
    body = _body()

    first = ingest_signed_gitlab_webhook(
        repository_id=REPOSITORY_ID,
        raw_body=body,
        headers=_headers(body),
        dependencies=dependencies,
    )
    second = ingest_signed_gitlab_webhook(
        repository_id=REPOSITORY_ID,
        raw_body=body,
        headers=_headers(body),
        dependencies=dependencies,
    )

    assert first.id == second.id
    assert first.payload_digest == "sha256:" + hashlib.sha256(body).hexdigest()
    assert first.ref == "refs/heads/feat/wi-601-source-control"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-gitlab-token": "legacy-token"},
        {
            "webhook-id": WEBHOOK_ID,
            "webhook-timestamp": str(int(NOW.timestamp())),
            "webhook-signature": "v1,invalid",
        },
    ],
)
def test_invalid_or_legacy_only_signature_never_persists(
    isolated_source_control_rw_engine: Engine,
    headers: dict[str, str],
) -> None:
    dependencies = _dependencies(isolated_source_control_rw_engine)
    _register(isolated_source_control_rw_engine, dependencies)

    with pytest.raises(WebhookSignatureInvalid):
        ingest_signed_gitlab_webhook(
            repository_id=REPOSITORY_ID,
            raw_body=_body(),
            headers=headers,
            dependencies=dependencies,
        )
    with isolated_source_control_rw_engine.connect() as db:
        count = db.exec_driver_sql("SELECT count(*) FROM source_control.webhook_inbox").scalar_one()

    assert count == 0


def test_multiple_signatures_accept_when_one_constant_time_value_matches() -> None:
    body = _body()
    headers = _headers(body)
    headers["webhook-signature"] = "v1,invalid " + headers["webhook-signature"]

    verified = verify_gitlab_standard_webhook(
        signing_token=SIGNING_TOKEN,
        webhook_id=headers["webhook-id"],
        timestamp=headers["webhook-timestamp"],
        signature_header=headers["webhook-signature"],
        raw_body=body,
        now=NOW,
        replay_window=timedelta(minutes=5),
    )

    assert verified.webhook_id == WEBHOOK_ID


def test_stale_timestamp_is_rejected() -> None:
    body = _body()
    stale = int((NOW - timedelta(minutes=6)).timestamp())

    with pytest.raises(WebhookReplayRejected):
        verify_gitlab_standard_webhook(
            signing_token=SIGNING_TOKEN,
            webhook_id=WEBHOOK_ID,
            timestamp=str(stale),
            signature_header=_sign(body, timestamp=stale),
            raw_body=body,
            now=NOW,
            replay_window=timedelta(minutes=5),
        )


def test_invalid_whsec_encoding_is_rejected_without_fallback() -> None:
    body = _body()

    with pytest.raises(WebhookSignatureInvalid):
        verify_gitlab_standard_webhook(
            signing_token="whsec_not-base64!",
            webhook_id=WEBHOOK_ID,
            timestamp=str(int(NOW.timestamp())),
            signature_header=_sign(body),
            raw_body=body,
            now=NOW,
            replay_window=timedelta(minutes=5),
        )


def test_same_webhook_id_with_different_digest_is_security_conflict(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies = _dependencies(isolated_source_control_rw_engine)
    _register(isolated_source_control_rw_engine, dependencies)
    first_body = _body()
    second_body = _body(after="c" * 40)
    ingest_signed_gitlab_webhook(
        repository_id=REPOSITORY_ID,
        raw_body=first_body,
        headers=_headers(first_body),
        dependencies=dependencies,
    )

    with pytest.raises(WebhookIdConflict):
        ingest_signed_gitlab_webhook(
            repository_id=REPOSITORY_ID,
            raw_body=second_body,
            headers=_headers(second_body),
            dependencies=dependencies,
        )


def test_project_mismatch_is_rejected_after_valid_signature(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies = _dependencies(isolated_source_control_rw_engine)
    _register(isolated_source_control_rw_engine, dependencies)
    body = _body(project_id=999)

    with pytest.raises(WebhookPayloadInvalid):
        ingest_signed_gitlab_webhook(
            repository_id=REPOSITORY_ID,
            raw_body=body,
            headers=_headers(body),
            dependencies=dependencies,
        )


def test_connector_route_returns_only_sanitized_acceptance(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies = _dependencies(isolated_source_control_rw_engine)
    _register(isolated_source_control_rw_engine, dependencies)
    app = FastAPI()
    app.include_router(
        create_webhook_router(lambda: SourceControlWebhookRuntime(dependencies=dependencies))
    )
    body = _body()

    response = TestClient(app).post(
        f"/webhooks/gitlab/{REPOSITORY_ID}",
        content=body,
        headers=_headers(body),
    )

    assert response.status_code == 202
    assert set(response.json()) == {"inboxId", "state"}
    assert SIGNING_TOKEN not in response.text
    assert body.decode() not in response.text
