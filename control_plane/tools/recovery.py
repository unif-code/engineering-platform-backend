from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, TextIO
from uuid import uuid4

from sqlalchemy import Engine, text

import control_plane.app.modules.identity as identity
from control_plane.app.modules.identity import IdentityDependencies


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recover the last unavailable Super Admin account.",
    )
    parser.add_argument("--employee-no", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--credentials-lost", action="store_true")
    return parser


def _runtime() -> tuple[Engine, IdentityDependencies, Any]:
    from control_plane.app.bootstrap.app import (
        identity_dependencies,
        identity_runtime_engine,
        security_change_orchestrator,
    )

    return (
        identity_runtime_engine(),
        identity_dependencies(),
        security_change_orchestrator(),
    )


def _write_evidence(evidence: TextIO, payload: dict[str, str]) -> None:
    evidence.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _write_evidence_safely(evidence: TextIO, payload: dict[str, str]) -> None:
    try:
        _write_evidence(evidence, payload)
    except Exception:
        # Evidence delivery cannot make a possibly committed credential change
        # look unexecuted. The database Audit remains the authoritative fact.
        pass


def _resolve_committed(
    engine: Engine,
    *,
    args: argparse.Namespace,
    expires_at: datetime,
    dependencies: IdentityDependencies,
) -> identity.SuperAdminCliExecution | None:
    with engine.begin() as db:
        return identity.resolve_recovery_cli(
            db,
            employee_no=args.employee_no,
            reason=args.reason,
            scope=args.scope,
            expires_at=expires_at,
            credentials_lost=args.credentials_lost,
            dependencies=dependencies,
        )


def _transaction_status(engine: Engine, source_transaction_id: str) -> str:
    try:
        with engine.connect() as db:
            return str(
                db.execute(
                    text("SELECT pg_xact_status(CAST(:source_xid AS xid8))"),
                    {"source_xid": source_transaction_id},
                ).scalar_one()
            )
    except Exception:
        return "unknown"


def _record_denial(
    engine: Engine,
    *,
    employee_no: str,
    reason_code: str,
    command_id: str,
    dependencies: IdentityDependencies,
    evidence: TextIO,
) -> int:
    try:
        with engine.begin() as db:
            identity.record_super_admin_recovery_denial(
                db,
                employee_no=employee_no,
                reason_code=reason_code,
                correlation_id=command_id,
                dependencies=dependencies,
            )
    except Exception:
        _write_evidence_safely(
            evidence,
            {
                "event": "super_admin_recovery",
                "result": "FAILED",
                "reasonCode": "DENIAL_EVIDENCE_FAILED",
                "commandId": command_id,
            },
        )
        return 4
    _write_evidence_safely(
        evidence,
        {
            "event": "super_admin_recovery",
            "result": "DENIED",
            "reasonCode": reason_code,
            "commandId": command_id,
        },
    )
    return 3


def main(
    argv: list[str] | None = None,
    *,
    engine: Engine | None = None,
    dependencies: IdentityDependencies | None = None,
    security_changes: Any = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    output = stdout or sys.stdout
    evidence = stderr or sys.stderr
    if engine is None or dependencies is None:
        runtime_engine, runtime_dependencies, runtime_security_changes = _runtime()
        engine = engine or runtime_engine
        dependencies = dependencies or runtime_dependencies
        if security_changes is None:
            security_changes = runtime_security_changes

    denial_command_id = f"cli-{uuid4().hex}"
    try:
        expires_at = datetime.fromisoformat(args.expires_at)
    except ValueError:
        return _record_denial(
            engine,
            employee_no=args.employee_no,
            reason_code="INVALID_EXPIRY",
            command_id=denial_command_id,
            dependencies=dependencies,
            evidence=evidence,
        )

    execution = None
    commit_attempted = False
    try:
        with engine.connect() as db:
            transaction = db.begin()
            source_transaction_id = ""
            commit_error: Exception | None = None
            try:
                source_transaction_id = str(
                    db.execute(text("SELECT pg_current_xact_id()")).scalar_one()
                )
                execution = identity.recover_super_admin_cli(
                    db,
                    employee_no=args.employee_no,
                    reason=args.reason,
                    scope=args.scope,
                    expires_at=expires_at,
                    credentials_lost=args.credentials_lost,
                    source_transaction_id=source_transaction_id,
                    dependencies=dependencies,
                )
                output.write(f"{execution.temporary_password}\n")
                output.flush()
                commit_attempted = True
                try:
                    transaction.commit()
                except Exception as error:
                    commit_error = error
            except Exception:
                if transaction.is_active:
                    transaction.rollback()
                raise
        assert execution is not None
        if commit_error is not None:
            try:
                resolved = _resolve_committed(
                    engine,
                    args=args,
                    expires_at=expires_at,
                    dependencies=dependencies,
                )
            except Exception:
                resolved = None
            if resolved is None:
                status = _transaction_status(engine, source_transaction_id)
                if status == "aborted":
                    _write_evidence_safely(
                        evidence,
                        {"event": "super_admin_recovery", "result": "FAILED"},
                    )
                    return 4
                _write_evidence_safely(
                    evidence,
                    {
                        "event": "super_admin_recovery",
                        "result": "OUTCOME_UNKNOWN",
                        "commandId": execution.correlation_id,
                    },
                )
                return 0
            if resolved.temporary_password != execution.temporary_password:
                _write_evidence_safely(
                    evidence,
                    {
                        "event": "super_admin_recovery",
                        "result": "OUTCOME_UNKNOWN",
                        "commandId": execution.correlation_id,
                    },
                )
                return 0
        _write_evidence_safely(
            evidence,
            {
                "event": "super_admin_recovery",
                "result": "SUCCESS",
                "employeeNo": args.employee_no,
                "scope": args.scope,
                "expiresAt": expires_at.isoformat(),
                "commandId": execution.correlation_id,
            },
        )
    except identity.SuperAdminRecoveryDenied as error:
        if commit_attempted:
            _write_evidence_safely(
                evidence,
                {
                    "event": "super_admin_recovery",
                    "result": "OUTCOME_UNKNOWN",
                    **({"commandId": execution.correlation_id} if execution is not None else {}),
                },
            )
            return 0
        return _record_denial(
            engine,
            employee_no=args.employee_no,
            reason_code=error.reason_code,
            command_id=denial_command_id,
            dependencies=dependencies,
            evidence=evidence,
        )
    except Exception:
        if commit_attempted:
            _write_evidence_safely(
                evidence,
                {
                    "event": "super_admin_recovery",
                    "result": "OUTCOME_UNKNOWN",
                    **({"commandId": execution.correlation_id} if execution is not None else {}),
                },
            )
            return 0
        _write_evidence_safely(
            evidence,
            {"event": "super_admin_recovery", "result": "FAILED"},
        )
        return 4

    if security_changes is not None and execution is not None:
        try:
            for ticket in execution.tickets:
                security_changes.complete(ticket)
            security_changes.reconcile_pending()
        except Exception:
            # The identity transaction is already committed and the durable fence
            # remains fail-closed. Reporting failure here would invite a second
            # credential issuance even though recovery already executed.
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
