import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text

from control_plane.app.modules.configuration import ConfigurationDependencies

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("configuration_seed")]


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 12, 8, 30, tzinfo=UTC)


class _Random:
    def __init__(self) -> None:
        self.value = 0

    def uuid4(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


def _dependencies() -> ConfigurationDependencies:
    from control_plane.app.modules.audit.adapters.transactional import (
        SqlAlchemyTransactionalAuditAppender,
    )

    return ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )


@contextmanager
def _rollback(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as db:
        transaction = db.begin()
        try:
            yield db
        finally:
            transaction.rollback()


def test_active_snapshot_returns_seeded_identity_version_one(
    configuration_rw_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import active_snapshot

    with configuration_rw_engine.connect() as db:
        snapshot = active_snapshot(db, "identity")

    assert snapshot.version == 1
    assert snapshot.schema_revision == 1
    assert snapshot.snapshot_hash == (
        "0406fd566b249b81c2c260833d56264c728171c38cba10c104781f7142ed3cb8"
    )
    assert snapshot.values == {
        "identity.temp_credential_ttl": 24,
        "identity.password_max_age": "NEVER",
        "identity.session_cap": 3,
        "identity.session_idle_timeout": 60,
        "identity.login_backoff": {
            "failureThreshold": 5,
            "initialDelaySeconds": 30,
            "maximumDelaySeconds": 900,
            "resetAfterHours": 24,
        },
        "identity.totp_attempt_cap": 5,
        "identity.draft_archive_after": 30,
    }


def test_active_snapshot_fails_closed_when_the_stored_hash_is_tampered(
    configuration_owner_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import (
        PolicySnapshotUnavailable,
        active_snapshot,
    )

    with _rollback(configuration_owner_engine) as db:
        db.execute(
            text(
                "UPDATE identity.version SET snapshot_hash=:invalid "
                "WHERE namespace='identity' AND scope='PLATFORM' AND version=1"
            ),
            {"invalid": "0" * 64},
        )
        with pytest.raises(PolicySnapshotUnavailable):
            active_snapshot(db, "identity")


@pytest.mark.parametrize(
    "invalid_case",
    [
        "missing_key",
        "extra_key",
        "wrong_type",
        "out_of_range",
        "unrepresentable",
        "unsupported_revision",
    ],
)
def test_public_active_snapshot_fails_closed_for_well_hashed_invalid_candidate(
    configuration_owner_engine: Engine,
    invalid_case: str,
) -> None:
    from control_plane.app.modules.configuration import (
        PolicySnapshotUnavailable,
        active_snapshot,
    )

    with _rollback(configuration_owner_engine) as db:
        snapshot = dict(
            db.execute(
                text(
                    "SELECT snapshot FROM identity.version "
                    "WHERE namespace='identity' AND scope='PLATFORM' AND version=1"
                )
            ).scalar_one()
        )
        schema_revision = 1
        if invalid_case == "missing_key":
            snapshot.pop("identity.session_idle_timeout")
        elif invalid_case == "extra_key":
            snapshot["identity.unknown"] = 1
        elif invalid_case == "wrong_type":
            snapshot["identity.session_idle_timeout"] = "60"
        elif invalid_case == "out_of_range":
            snapshot["identity.session_idle_timeout"] = 10
        elif invalid_case == "unrepresentable":
            snapshot["identity.temp_credential_ttl"] = 10**20
        elif invalid_case == "unsupported_revision":
            schema_revision = 2
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
                "WHERE namespace='identity' AND scope='PLATFORM' AND version=1"
            ),
            {
                "snapshot": json.dumps(snapshot, separators=(",", ":")),
                "snapshot_hash": hashlib.sha256(canonical).hexdigest(),
                "schema_revision": schema_revision,
            },
        )
        with pytest.raises(PolicySnapshotUnavailable):
            active_snapshot(db, "identity")


def test_active_snapshot_fails_closed_when_no_pointer_exists(
    configuration_rw_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import (
        PolicySnapshotUnavailable,
        active_snapshot,
    )

    with configuration_rw_engine.connect() as db:
        with pytest.raises(PolicySnapshotUnavailable):
            active_snapshot(db, "unknown")


def test_catalog_returns_the_seven_typed_identity_keys(
    configuration_rw_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import catalog

    with configuration_rw_engine.connect() as db:
        keys = catalog(db, "identity")

    assert [item.key for item in keys] == [
        "identity.draft_archive_after",
        "identity.login_backoff",
        "identity.password_max_age",
        "identity.session_cap",
        "identity.session_idle_timeout",
        "identity.temp_credential_ttl",
        "identity.totp_attempt_cap",
    ]
    idle = next(item for item in keys if item.key == "identity.session_idle_timeout")
    assert idle.model_dump() == {
        "key": "identity.session_idle_timeout",
        "namespace": "identity",
        "value_type": "INTEGER",
        "unit": "MINUTES",
        "default_value": 60,
        "min_value": 15,
        "max_value": 240,
        "enum_values": None,
        "effect_semantics": "IMMEDIATE",
        "schema_revision": 1,
    }


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("unit", "HOURS"),
        ("effect_semantics", "NEW_OBJECT"),
    ],
)
def test_active_snapshot_fails_closed_when_owner_catalog_metadata_drifts(
    configuration_owner_engine: Engine,
    column: str,
    value: str,
) -> None:
    from control_plane.app.modules.configuration import (
        PolicySnapshotUnavailable,
        active_snapshot,
    )

    assert column in {"unit", "effect_semantics"}
    with _rollback(configuration_owner_engine) as db:
        db.execute(
            text(
                f"UPDATE identity.policy_key SET {column}=:value "
                "WHERE key='identity.session_idle_timeout'"
            ),
            {"value": value},
        )
        with pytest.raises(PolicySnapshotUnavailable):
            active_snapshot(db, "identity")


def test_draft_create_and_update_use_revision_etags_and_full_canonical_content(
    configuration_rw_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import (
        StaleDraftRevision,
        create_draft,
        update_draft,
    )

    with configuration_rw_engine.connect() as db:
        transaction = db.begin()
        dependencies = _dependencies()
        created = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 45},
            actor_id="admin-1",
            dependencies=dependencies,
        )
        updated = update_draft(
            db,
            namespace="identity",
            draft_id=created.id,
            values={"identity.session_idle_timeout": 30},
            actor_id="admin-1",
            expected_revision=1,
            dependencies=dependencies,
        )
        with pytest.raises(StaleDraftRevision):
            update_draft(
                db,
                namespace="identity",
                draft_id=created.id,
                values={"identity.session_idle_timeout": 20},
                actor_id="admin-1",
                expected_revision=1,
                dependencies=dependencies,
            )
        transaction.rollback()

    assert created.revision == 1
    assert created.base_version == 1
    assert len(created.content) == 7
    assert created.content["identity.session_idle_timeout"] == 45
    assert updated.revision == 2
    assert updated.content["identity.session_idle_timeout"] == 30
    assert updated.validation_evidence is None


def test_validation_is_bound_and_reports_idle_below_fifteen_without_echoing_values(
    configuration_rw_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import (
        create_draft,
        update_draft,
        validate_draft,
    )

    with configuration_rw_engine.connect() as db:
        transaction = db.begin()
        dependencies = _dependencies()
        created = create_draft(
            db,
            namespace="identity",
            values={},
            actor_id="admin-1",
            dependencies=dependencies,
        )
        updated = update_draft(
            db,
            namespace="identity",
            draft_id=created.id,
            values={"identity.session_idle_timeout": 10},
            actor_id="admin-1",
            expected_revision=1,
            dependencies=dependencies,
        )
        result = validate_draft(
            db,
            namespace="identity",
            draft_id=created.id,
            actor_id="admin-1",
            expected_revision=updated.revision,
            dependencies=dependencies,
        )
        active_version = db.execute(
            text(
                "SELECT version FROM identity.active_pointer "
                "WHERE namespace='identity' AND scope='PLATFORM'"
            )
        ).scalar_one()
        transaction.rollback()

    assert result.valid is False
    assert result.revision == 3
    assert result.content_hash == updated.content_hash
    assert result.issues[0].model_dump() == {
        "code": "BELOW_MINIMUM",
        "key": "identity.session_idle_timeout",
        "message": "Value is below the permitted minimum.",
    }
    assert active_version == 1


def test_draft_validation_observes_identity_owner_schema_without_a_local_rule_copy(
    configuration_rw_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import create_draft, validate_draft

    with _rollback(configuration_rw_engine) as db:
        dependencies = _dependencies()
        created = create_draft(
            db,
            namespace="identity",
            values={},
            actor_id="admin-owner-rule",
            dependencies=dependencies,
        )
        content = {**created.content, "identity.unknown": 1}
        canonical = json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        db.execute(
            text(
                "UPDATE identity.draft SET content=CAST(:content AS JSONB), "
                "content_hash=:content_hash WHERE id=:draft_id"
            ),
            {
                "content": json.dumps(content, separators=(",", ":")),
                "content_hash": hashlib.sha256(canonical).hexdigest(),
                "draft_id": created.id,
            },
        )

        result = validate_draft(
            db,
            namespace="identity",
            draft_id=created.id,
            actor_id="admin-owner-rule",
            expected_revision=created.revision,
            dependencies=dependencies,
        )

    assert result.valid is False
    assert [issue.model_dump() for issue in result.issues] == [
        {
            "code": "UNREGISTERED_KEY",
            "key": "identity.unknown",
            "message": "Policy key is not registered.",
        }
    ]
