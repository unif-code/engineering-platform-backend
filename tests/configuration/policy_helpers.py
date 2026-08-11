import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text


def snapshot_with_idle_minutes(minutes: int) -> tuple[dict[str, object], str]:
    snapshot: dict[str, object] = {
        "identity.temp_credential_ttl": 24,
        "identity.password_max_age": "NEVER",
        "identity.session_cap": 3,
        "identity.session_idle_timeout": minutes,
        "identity.login_backoff": {
            "failureThreshold": 5,
            "initialDelaySeconds": 30,
            "maximumDelaySeconds": 900,
            "resetAfterHours": 24,
        },
        "identity.totp_attempt_cap": 5,
        "identity.draft_archive_after": 30,
    }
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return snapshot, hashlib.sha256(canonical).hexdigest()


@contextmanager
def rollback_connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as db:
        transaction = db.begin()
        try:
            yield db
        finally:
            transaction.rollback()


@contextmanager
def temporary_active_snapshot(
    owner_engine: Engine,
    snapshot: dict[str, object],
    *,
    schema_revision: int = 1,
) -> Iterator[None]:
    with owner_engine.begin() as db:
        original = (
            db.execute(
                text(
                    "SELECT p.version, v.snapshot, v.snapshot_hash, v.schema_revision "
                    "FROM identity.active_pointer p JOIN identity.version v "
                    "USING (namespace, scope, version) "
                    "WHERE p.namespace='identity' AND p.scope='PLATFORM'"
                )
            )
            .mappings()
            .one()
        )
        canonical = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        db.execute(
            text(
                "UPDATE identity.version SET snapshot=CAST(:snapshot AS JSONB), "
                "snapshot_hash=:snapshot_hash, schema_revision=:schema_revision "
                "WHERE namespace='identity' AND scope='PLATFORM' AND version=:version"
            ),
            {
                "snapshot": json.dumps(snapshot, separators=(",", ":")),
                "snapshot_hash": hashlib.sha256(canonical).hexdigest(),
                "schema_revision": schema_revision,
                "version": original["version"],
            },
        )
    try:
        yield
    finally:
        with owner_engine.begin() as db:
            db.execute(
                text(
                    "UPDATE identity.version SET snapshot=CAST(:snapshot AS JSONB), "
                    "snapshot_hash=:snapshot_hash, schema_revision=:schema_revision "
                    "WHERE namespace='identity' AND scope='PLATFORM' AND version=:version"
                ),
                {
                    "snapshot": json.dumps(original["snapshot"], separators=(",", ":")),
                    "snapshot_hash": original["snapshot_hash"],
                    "schema_revision": original["schema_revision"],
                    "version": original["version"],
                },
            )
            db.execute(
                text(
                    "UPDATE identity.active_pointer SET version=:version "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                ),
                {"version": original["version"]},
            )


@contextmanager
def temporary_policy_key_default(
    owner_engine: Engine,
    *,
    key: str,
    default_value: object,
) -> Iterator[None]:
    with owner_engine.begin() as db:
        original = db.execute(
            text("SELECT default_value FROM identity.policy_key WHERE key=:key"),
            {"key": key},
        ).scalar_one()
        db.execute(
            text(
                "UPDATE identity.policy_key SET default_value=CAST(:default_value AS JSONB) "
                "WHERE key=:key"
            ),
            {
                "key": key,
                "default_value": json.dumps(default_value, separators=(",", ":")),
            },
        )
    try:
        yield
    finally:
        with owner_engine.begin() as db:
            db.execute(
                text(
                    "UPDATE identity.policy_key "
                    "SET default_value=CAST(:default_value AS JSONB) WHERE key=:key"
                ),
                {
                    "key": key,
                    "default_value": json.dumps(original, separators=(",", ":")),
                },
            )
