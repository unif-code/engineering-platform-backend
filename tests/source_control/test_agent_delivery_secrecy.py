import logging
import os
import secrets
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.sql.compiler import IdentifierPreparer

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.source_control import (
    AgentExecutionBindingSnapshot,
    AgentPushBindingRejected,
    AgentPushRequestSpec,
    authorize_agent_push,
    execute_agent_push,
)
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_agent_delivery_adapters import (
    BINDING_DIGEST,
    WORKSPACE_ID,
    _seed_branch,
)
from tests.source_control.test_agent_delivery_api import CapabilityGuard, _client
from tests.source_control.test_agent_delivery_commands import (
    FixedGrantIssuer,
    _dependencies,
    _spec,
)


class SentinelExecutionBindings:
    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token
        self.active = False

    def validate(
        self,
        spec: AgentPushRequestSpec,
        *,
        raw_fencing_token: str,
    ) -> AgentExecutionBindingSnapshot:
        if raw_fencing_token != self._expected_token or not self.active:
            raise AgentPushBindingRejected("Agent push binding was rejected")
        return AgentExecutionBindingSnapshot(
            attempt_id=spec.attempt_id,
            attempt_generation=spec.attempt_generation,
            execution_binding_digest=BINDING_DIGEST,
            requirement_id=spec.requirement_id,
            work_item_id=spec.work_item_id,
            workspace_id=spec.workspace_id,
            repository_id=spec.repository_id,
            branch_binding_id=spec.branch_binding_id,
            branch_name=spec.branch_name,
            active=True,
            fenced=False,
        )


def _database_contains(engine: Engine, needle: str) -> bool:
    preparer = IdentifierPreparer(engine.dialect)
    with engine.connect() as db:
        tables = db.execute(
            text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_type='BASE TABLE' "
                "AND table_schema NOT IN ('information_schema', 'pg_catalog')"
            )
        ).all()
        for schema_name, table_name in tables:
            qualified = (
                f"{preparer.quote_identifier(str(schema_name))}."
                f"{preparer.quote_identifier(str(table_name))}"
            )
            found = db.execute(
                text(
                    f"SELECT EXISTS (SELECT 1 FROM {qualified} AS candidate "
                    "WHERE position(:needle IN to_jsonb(candidate)::text) > 0)"
                ),
                {"needle": needle},
            ).scalar_one()
            if found:
                return True
    return False


def _source_files_contain(needle: str) -> bool:
    roots = tuple(
        Path(name) for name in ("control_plane", "migrations", "scripts", "tests", "docs")
    )
    suffixes = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
    files = [path for root in roots for path in root.rglob("*") if path.suffix in suffixes]
    files.extend(path for path in (Path("openapi.json"), Path("pyproject.toml")) if path.exists())
    return any(needle in path.read_text(encoding="utf-8", errors="ignore") for path in files)


def test_raw_grant_and_fencing_token_are_absent_from_all_observable_surfaces(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_engine = isolated_source_control_database.runtime
    owner_engine = isolated_source_control_database.owner
    raw_grant = f"grant-{secrets.token_urlsafe(48)}"
    raw_fencing_token = f"fence-{secrets.token_urlsafe(48)}"
    bindings = SentinelExecutionBindings(raw_fencing_token)
    _seed_branch(runtime_engine)
    dependencies, _broker, _bindings, _issuer, _clock, _audit = _dependencies(
        runtime_engine,
        bindings=bindings,  # type: ignore[arg-type]
        issuer=FixedGrantIssuer(raw_grant),
    )
    dependencies = replace(
        dependencies,
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    caplog.set_level(logging.DEBUG)
    bindings.active = True
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=raw_fencing_token,
        dependencies=dependencies,
    )
    bindings.active = False
    with pytest.raises(AgentPushBindingRejected) as captured:
        execute_agent_push(
            grant.delivery.id,
            raw_grant=raw_grant,
            raw_fencing_token=raw_fencing_token,
            dependencies=dependencies,
        )
    bindings.active = True
    delivered = execute_agent_push(
        grant.delivery.id,
        raw_grant=raw_grant,
        raw_fencing_token=raw_fencing_token,
        dependencies=dependencies,
    )
    response = _client(
        runtime_engine,
        dependencies=dependencies,
        guard=CapabilityGuard({("requirement.read", WORKSPACE_ID)}),
    ).get(f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{delivered.id}")

    with owner_engine.connect() as db:
        audit_text = "\n".join(
            db.execute(
                text(
                    "SELECT row_to_json(event)::text FROM audit.audit_event AS event "
                    "WHERE target_id=:request_id ORDER BY id"
                ),
                {"request_id": delivered.id},
            ).scalars()
        )
        fact_text = "\n".join(
            db.execute(
                text(
                    "SELECT row_to_json(fact)::text FROM "
                    "source_control.agent_delivery_fact AS fact "
                    "WHERE push_request_id=:request_id ORDER BY id"
                ),
                {"request_id": delivered.id},
            ).scalars()
        )

    observable_surfaces = (
        repr(grant),
        repr(delivered),
        str(captured.value),
        response.text,
        audit_text,
        fact_text,
        caplog.text,
        "\n".join(f"{key}={value}" for key, value in os.environ.items()),
        " ".join(sys.argv),
    )
    for sentinel in (raw_grant, raw_fencing_token):
        assert all(sentinel not in surface for surface in observable_surfaces)
        assert not _database_contains(owner_engine, sentinel)
        assert not _source_files_contain(sentinel)
